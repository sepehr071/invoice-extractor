"""SQLite layer for the HITL invoice reviewer.

One DB file at ``data/app.db``. Stdlib ``sqlite3`` with
``check_same_thread=False`` so FastAPI's threadpool can hit it.

bbox columns store JSON-encoded ``Polygon`` (``list[list[int]]``); decoded on
read. ``line_items.data`` stores the full ``LineItem`` as JSON.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable

from app import storage

DB_PATH = storage.DATA_ROOT / "app.db"


# ---------- connection ----------------------------------------------------

def get_conn() -> sqlite3.Connection:
    """Return a connection with ``check_same_thread=False`` + ``Row`` factory.

    WAL + a 5s busy_timeout let the background worker and request threadpool
    write concurrently without surfacing ``database is locked`` errors: WAL
    allows one writer alongside many readers, and busy_timeout makes a second
    writer wait-and-retry instead of failing immediately. ``synchronous=NORMAL``
    is the safe, fast pairing for WAL.
    """
    storage.ensure_dirs()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- schema --------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    source_filename TEXT NOT NULL,
    page_count      INTEGER,
    status          TEXT NOT NULL,
    progress_pct    INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    language        TEXT NOT NULL DEFAULT 'en',
    mode            TEXT NOT NULL DEFAULT 'invoice',
    prompt          TEXT,
    output_format   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fields (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    field_path  TEXT NOT NULL,
    value       TEXT,
    page        INTEGER,
    bbox        TEXT,
    match_score REAL NOT NULL DEFAULT 0,
    edited      INTEGER NOT NULL DEFAULT 0,
    UNIQUE(job_id, field_path)
);

CREATE TABLE IF NOT EXISTS line_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    row_index   INTEGER NOT NULL,
    data        TEXT NOT NULL,
    page        INTEGER,
    bbox        TEXT,
    edited      INTEGER NOT NULL DEFAULT 0,
    UNIQUE(job_id, row_index)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  TEXT NOT NULL,
    action  TEXT NOT NULL,
    payload TEXT,
    ts      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sanctions_results (
    job_id        TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    status        TEXT NOT NULL,
    checked_at    TEXT NOT NULL,
    list_version  TEXT,
    error         TEXT,
    matches_json  TEXT NOT NULL,
    llm_model     TEXT,
    llm_error     TEXT
);

CREATE INDEX IF NOT EXISTS idx_fields_job     ON fields(job_id);
CREATE INDEX IF NOT EXISTS idx_line_items_job ON line_items(job_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_job  ON audit_log(job_id);
"""


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """True when `column` exists on `table` (via PRAGMA table_info)."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def init_db() -> None:
    """Idempotently create tables + apply additive migrations."""
    storage.ensure_dirs()
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        # Additive migration for databases that predate the jobs.language
        # column. Guarded by PRAGMA table_info so it is a no-op once applied.
        if not _has_column(conn, "jobs", "language"):
            conn.execute("ALTER TABLE jobs ADD COLUMN language TEXT DEFAULT 'en'")
        # Additive migrations for databases that predate the generalized
        # OCR-platform columns (mode / prompt / output_format). Same PRAGMA
        # guard so each is a no-op once applied; old rows default to 'invoice'.
        if not _has_column(conn, "jobs", "mode"):
            conn.execute("ALTER TABLE jobs ADD COLUMN mode TEXT NOT NULL DEFAULT 'invoice'")
        if not _has_column(conn, "jobs", "prompt"):
            conn.execute("ALTER TABLE jobs ADD COLUMN prompt TEXT")
        if not _has_column(conn, "jobs", "output_format"):
            conn.execute("ALTER TABLE jobs ADD COLUMN output_format TEXT")
        # Additive migrations for existing databases that predate the
        # llm_model / llm_error columns. Safe to attempt — SQLite raises
        # OperationalError when the column already exists; swallow that.
        for stmt in (
            "ALTER TABLE sanctions_results ADD COLUMN llm_model TEXT",
            "ALTER TABLE sanctions_results ADD COLUMN llm_error TEXT",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass


# ---------- jobs ----------------------------------------------------------

def create_job(
    job_id: str,
    filename: str,
    page_count: int | None = None,
    language: str = "en",
    mode: str = "invoice",
    prompt: str | None = None,
    output_format: str | None = None,
) -> None:
    """Insert a new job row in status 'created'.

    The web upload renders pages synchronously before this is called, so
    `page_count` is known up front; CLI/legacy callers can pass None and
    update it later via `update_job_status`. `language` ("en" | "fa") selects
    the OCR recognizer and can be overridden later via `set_job_language`.
    `mode` ("invoice" | "prompt" | ...) selects the extraction path; `prompt`
    and `output_format` carry the free-form instruction + desired result shape
    for non-invoice modes, all overridable later via `set_job_mode`.
    """
    now = _now_iso()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO jobs (id, source_filename, page_count, status, progress_pct,"
            " error, language, mode, prompt, output_format, created_at, updated_at)"
            " VALUES (?, ?, ?, 'created', 0, NULL, ?, ?, ?, ?, ?, ?)",
            (job_id, filename, page_count, language, mode, prompt, output_format, now, now),
        )


def set_job_language(job_id: str, language: str) -> None:
    """Update a job's OCR language ("en" | "fa")."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET language = ?, updated_at = ? WHERE id = ?",
            (language, _now_iso(), job_id),
        )


def get_job_language(job_id: str) -> str:
    """Return a job's OCR language, defaulting to 'en' when unset/missing."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT language FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    if row is None or row["language"] is None:
        return "en"
    return str(row["language"])


def set_job_mode(
    job_id: str,
    mode: str,
    prompt: str | None = None,
    output_format: str | None = None,
) -> None:
    """Update a job's extraction mode + prompt/output_format in one write."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE jobs SET mode = ?, prompt = ?, output_format = ?, updated_at = ?"
            " WHERE id = ?",
            (mode, prompt, output_format, _now_iso(), job_id),
        )


def get_job_mode(job_id: str) -> str:
    """Return a job's extraction mode, defaulting to 'invoice' when unset/missing."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT mode FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    if row is None or row["mode"] is None:
        return "invoice"
    return str(row["mode"])


def get_job_prompt(job_id: str) -> str | None:
    """Return a job's free-form extraction prompt, or None when unset/missing."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT prompt FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    if row is None:
        return None
    return row["prompt"]


def get_job_output_format(job_id: str) -> str | None:
    """Return a job's desired result shape, or None when unset/missing."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT output_format FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
    if row is None:
        return None
    return row["output_format"]


def get_job(job_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None


def list_jobs() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def update_job_status(
    job_id: str,
    status: str,
    progress_pct: int | None = None,
    error: str | None = None,
    page_count: int | None = None,
) -> None:
    sets = ["status = ?", "updated_at = ?"]
    args: list[Any] = [status, _now_iso()]
    if progress_pct is not None:
        sets.append("progress_pct = ?")
        args.append(progress_pct)
    if error is not None:
        sets.append("error = ?")
        args.append(error)
    if page_count is not None:
        sets.append("page_count = ?")
        args.append(page_count)
    args.append(job_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", args)


# ---------- fields --------------------------------------------------------

def _dumps_bbox(bbox: list[list[int]] | None) -> str | None:
    return json.dumps(bbox) if bbox is not None else None


def _loads_bbox(raw: str | None) -> list[list[int]] | None:
    return json.loads(raw) if raw else None


def upsert_field_link(
    job_id: str,
    field_path: str,
    value: Any,
    page: int | None,
    bbox: list[list[int]] | None,
    match_score: float,
    edited: bool = False,
) -> None:
    """Insert or replace a field link. Used by the linker and on PATCH."""
    value_str = None if value is None else str(value)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO fields (job_id, field_path, value, page, bbox, match_score, edited)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(job_id, field_path) DO UPDATE SET"
            "   value = excluded.value,"
            "   page = excluded.page,"
            "   bbox = excluded.bbox,"
            "   match_score = excluded.match_score,"
            "   edited = excluded.edited",
            (
                job_id,
                field_path,
                value_str,
                page,
                _dumps_bbox(bbox),
                float(match_score),
                1 if edited else 0,
            ),
        )


def _row_to_field(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "field_path": row["field_path"],
        "value": row["value"],
        "page": row["page"],
        "bbox": _loads_bbox(row["bbox"]),
        "score": float(row["match_score"]),
        "edited": bool(row["edited"]),
    }


def list_fields(job_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM fields WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()
        return [_row_to_field(r) for r in rows]


def get_field(job_id: str, field_path: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM fields WHERE job_id = ? AND field_path = ?",
            (job_id, field_path),
        ).fetchone()
        return _row_to_field(row) if row else None


def patch_field(
    job_id: str,
    field_path: str,
    value: Any = ...,
    page: int | None = ...,
    bbox: list[list[int]] | None = ...,
) -> dict[str, Any] | None:
    """Apply a partial update; ``...`` (Ellipsis) means "do not change".

    Marks ``edited = 1``. Returns the updated row or None if missing.
    """
    sets: list[str] = ["edited = 1"]
    args: list[Any] = []
    if value is not ...:
        sets.append("value = ?")
        args.append(None if value is None else str(value))
    if page is not ...:
        sets.append("page = ?")
        args.append(page)
    if bbox is not ...:
        sets.append("bbox = ?")
        args.append(_dumps_bbox(bbox))
    args.extend([job_id, field_path])
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE fields SET {', '.join(sets)} WHERE job_id = ? AND field_path = ?",
            args,
        )
        if cur.rowcount == 0:
            return None
    return get_field(job_id, field_path)


# ---------- line items ----------------------------------------------------

def _row_to_line_item(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "row_index": int(row["row_index"]),
        "data": json.loads(row["data"]),
        "page": row["page"],
        "bbox": _loads_bbox(row["bbox"]),
        "edited": bool(row["edited"]),
    }


def list_line_items(job_id: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM line_items WHERE job_id = ? ORDER BY row_index",
            (job_id,),
        ).fetchall()
        return [_row_to_line_item(r) for r in rows]


def get_line_item(job_id: str, row_index: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM line_items WHERE job_id = ? AND row_index = ?",
            (job_id, row_index),
        ).fetchone()
        return _row_to_line_item(row) if row else None


def add_line_item(
    job_id: str,
    data: dict[str, Any],
    page: int | None = None,
    bbox: list[list[int]] | None = None,
    edited: bool = False,
    row_index: int | None = None,
) -> dict[str, Any]:
    """Insert a line item. If ``row_index`` is None, server assigns max+1."""
    with get_conn() as conn:
        if row_index is None:
            cur = conn.execute(
                "SELECT COALESCE(MAX(row_index) + 1, 0) AS next FROM line_items WHERE job_id = ?",
                (job_id,),
            )
            row_index = int(cur.fetchone()["next"])
        conn.execute(
            "INSERT INTO line_items (job_id, row_index, data, page, bbox, edited)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                job_id,
                row_index,
                json.dumps(data, ensure_ascii=False),
                page,
                _dumps_bbox(bbox),
                1 if edited else 0,
            ),
        )
    return get_line_item(job_id, row_index)  # type: ignore[return-value]


def patch_line_item(
    job_id: str,
    row_index: int,
    data: dict[str, Any] | None = ...,  # type: ignore[assignment]
    page: int | None = ...,
    bbox: list[list[int]] | None = ...,
) -> dict[str, Any] | None:
    sets: list[str] = ["edited = 1"]
    args: list[Any] = []
    if data is not ...:
        sets.append("data = ?")
        args.append(json.dumps(data, ensure_ascii=False))
    if page is not ...:
        sets.append("page = ?")
        args.append(page)
    if bbox is not ...:
        sets.append("bbox = ?")
        args.append(_dumps_bbox(bbox))
    args.extend([job_id, row_index])
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE line_items SET {', '.join(sets)} WHERE job_id = ? AND row_index = ?",
            args,
        )
        if cur.rowcount == 0:
            return None
    return get_line_item(job_id, row_index)


def delete_line_item(job_id: str, row_index: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM line_items WHERE job_id = ? AND row_index = ?",
            (job_id, row_index),
        )
        return cur.rowcount > 0


# ---------- sanctions ----------------------------------------------------

def upsert_sanctions_result(job_id: str, verdict: Any) -> None:
    """Persist (or replace) the sanctions verdict for a job.

    `verdict` must be a `SanctionsVerdict` Pydantic model (typed as `Any`
    to keep this module free of an import cycle on `app.models`).
    """
    payload = verdict.model_dump()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO sanctions_results"
            " (job_id, status, checked_at, list_version, error, matches_json, llm_model, llm_error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(job_id) DO UPDATE SET"
            "   status = excluded.status,"
            "   checked_at = excluded.checked_at,"
            "   list_version = excluded.list_version,"
            "   error = excluded.error,"
            "   matches_json = excluded.matches_json,"
            "   llm_model = excluded.llm_model,"
            "   llm_error = excluded.llm_error",
            (
                job_id,
                payload.get("status"),
                payload.get("checked_at"),
                payload.get("list_version"),
                payload.get("error"),
                json.dumps(payload.get("matches") or [], ensure_ascii=False),
                payload.get("llm_model"),
                payload.get("llm_error"),
            ),
        )


def get_sanctions_result(job_id: str) -> dict[str, Any] | None:
    """Return the stored verdict as a dict matching `SanctionsVerdict`, or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status, checked_at, list_version, error, matches_json, llm_model, llm_error"
            " FROM sanctions_results WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    if row is None:
        return None
    try:
        matches = json.loads(row["matches_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        matches = []
    return {
        "status": row["status"],
        "checked_at": row["checked_at"],
        "list_version": row["list_version"],
        "error": row["error"],
        "matches": matches,
        "llm_model": row["llm_model"] if "llm_model" in row.keys() else None,
        "llm_error": row["llm_error"] if "llm_error" in row.keys() else None,
    }


# ---------- audit log ----------------------------------------------------

def audit_log(job_id: str, action: str, payload: Any | None = None) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO audit_log (job_id, action, payload, ts) VALUES (?, ?, ?, ?)",
            (
                job_id,
                action,
                json.dumps(payload, ensure_ascii=False, default=str) if payload is not None else None,
                _now_iso(),
            ),
        )


# ---------- bulk replace helpers (used by worker / approval) -------------

def replace_fields(job_id: str, links: Iterable[dict[str, Any]]) -> None:
    """Replace all field links for a job (used after linking finishes)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM fields WHERE job_id = ?", (job_id,))
        conn.executemany(
            "INSERT INTO fields (job_id, field_path, value, page, bbox, match_score, edited)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    job_id,
                    link["field_path"],
                    None if link.get("value") is None else str(link["value"]),
                    link.get("page"),
                    _dumps_bbox(link.get("bbox")),
                    float(link.get("score", 0.0)),
                    1 if link.get("edited", False) else 0,
                )
                for link in links
            ],
        )


def replace_line_items(job_id: str, items: Iterable[dict[str, Any]]) -> None:
    """Replace all line items for a job (used after linking finishes)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM line_items WHERE job_id = ?", (job_id,))
        conn.executemany(
            "INSERT INTO line_items (job_id, row_index, data, page, bbox, edited)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    job_id,
                    int(item["row_index"]),
                    json.dumps(item["data"], ensure_ascii=False),
                    item.get("page"),
                    _dumps_bbox(item.get("bbox")),
                    1 if item.get("edited", False) else 0,
                )
                for item in items
            ],
        )
