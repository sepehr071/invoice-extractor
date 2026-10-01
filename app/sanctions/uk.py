"""UK Sanctions List (UKSL): download, cache, parse.

The FCDO publishes the single UK Sanctions List — which superseded the OFSI
"Consolidated List of asset-freeze targets" on 28 Jan 2026 — in several
formats. We consume the flat **CSV**: its columns are stable and documented,
and parsing CSV is far more robust than tracking the evolving XML schema
(``SanctionsListSchema-4.x``). The CSV has one row per name variation, grouped
by the designation's ``Unique ID``.

Records are exposed as ``UkEntry``, structurally compatible with
``loader.SdnEntry`` so ``match.py`` treats every list uniformly.
``treasury_url`` points at the published UK Sanctions List page (the list
exposes no per-entry URL). Enabled by default; disable with
``UK_SANCTIONS_ENABLED=0``.
"""

from __future__ import annotations

import csv
import functools
import hashlib
import io
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app import storage
from app.sanctions.loader import _detect_script

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

UK_CSV_URL = os.environ.get(
    "UK_SANCTIONS_CSV_URL",
    "https://sanctionslist.fcdo.gov.uk/docs/UK-Sanctions-List.csv",
)
UK_LIST_PAGE = "https://www.gov.uk/government/publications/the-uk-sanctions-list"
USER_AGENT = "invoice-ai/0.1 (sanctions-screener)"
DOWNLOAD_TIMEOUT_SEC = 120

SOURCE_LABEL = "UK_SANCTIONS"


def _uk_dir() -> Path:
    return storage.DATA_ROOT / "uk"


def _csv_path() -> Path:
    return _uk_dir() / "uk_sanctions_list.csv"


def _refresh_marker_path() -> Path:
    return _uk_dir() / "_last_refresh.json"


# ---------------------------------------------------------------------------
# Entry model — structurally compatible with loader.SdnEntry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UkEntry:
    """One UK Sanctions List designation, projected to the SdnEntry surface."""

    uid: int
    primary_name: str
    sdn_type: str  # "Entity" | "Individual" | "Vessel" | "Unknown"
    programs: tuple[str, ...]
    aliases: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    address_countries: tuple[str, ...] = field(default_factory=tuple)
    list_date: str | None = None
    source_list: str = SOURCE_LABEL
    primary_name_native: str | None = None
    aliases_native: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)
    addresses: tuple[dict, ...] = field(default_factory=tuple)
    ids: tuple[dict, ...] = field(default_factory=tuple)
    relationships: tuple[dict, ...] = field(default_factory=tuple)
    # UK reference id (textual, e.g. "GBR0001"); kept for traceability.
    uk_unique_id: str = ""

    @property
    def treasury_url(self) -> str:
        return UK_LIST_PAGE


# ---------------------------------------------------------------------------
# Refresh marker helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_marker() -> dict:
    path = _refresh_marker_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_marker(payload: dict) -> None:
    path = _refresh_marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def _download_csv(url: str, target: Path) -> tuple[bool, int, str | None]:
    """Download the UK CSV atomically with bounded retry. Returns (ok, bytes, error)."""
    tmp = target.with_suffix(target.suffix + ".tmp")
    last_err: str | None = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SEC) as resp:
                data = resp.read()
            if not data:
                last_err = "empty response"
                continue
            head = data.lstrip()[:64].lower()
            if head.startswith(b"<!doctype") or head.startswith(b"<html"):
                last_err = "response is HTML, not CSV (download URL may have moved)"
                continue
            tmp.write_bytes(data)
            os.replace(tmp, target)
            return True, len(data), None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            time.sleep(2 * (attempt + 1))
    return False, 0, last_err


def refresh_uk_now() -> dict:
    """Download the UK Sanctions List CSV, write atomically, update the marker."""
    _uk_dir().mkdir(parents=True, exist_ok=True)
    ok, nbytes, err = _download_csv(UK_CSV_URL, _csv_path())
    _load_uk_entries_cached.cache_clear()
    list_version = _peek_list_version(_csv_path()) if ok else None
    payload = {
        "ok": ok,
        "fetched_at": _now_iso(),
        "list_version": list_version,
        "bytes": nbytes,
        "error": err,
    }
    _write_marker(payload)
    return payload


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def _stable_uid(unique_id: str) -> int:
    """Reproducible 32-bit uid from the UK textual reference (stable across runs)."""
    return int(hashlib.sha1(unique_id.encode("utf-8")).hexdigest()[:8], 16)


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return (row[idx] or "").strip()


def _map_type(value: str) -> str:
    v = value.lower()
    if "individual" in v:
        return "Individual"
    if "entity" in v:
        return "Entity"
    if "ship" in v or "vessel" in v:
        return "Vessel"
    return "Unknown"


def _resolve_columns(header: list[str]) -> dict:
    low = [(h or "").strip().lower() for h in header]

    def find(*needles: str, exclude: tuple[str, ...] = ()) -> int | None:
        for i, h in enumerate(low):
            if any(n in h for n in needles) and not any(x in h for x in exclude):
                return i
        return None

    name_cols: list[int] = []
    for k in range(1, 7):
        idx = find(f"name {k}")
        if idx is not None:
            name_cols.append(idx)

    return {
        "uid": find("unique id"),
        "ofsi_group": find("ofsi group id", "group id"),
        "type": find("individual", "entity, ship", "group type", exclude=("name",)),
        "name_type": find("name type", "alias type"),
        "regime": find("regime name", "regime"),
        "country": find("country", exclude=("birth", "nationality", "origin")),
        "designated": find("date designated", "designation date", "last updated"),
        "nonlatin": find("non-latin", "non latin"),
        "name_cols": name_cols,
    }


def _peek_list_version(path: Path) -> str | None:
    """Best-effort: pull a date out of the CSV preamble/first rows."""
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as fh:
            head = fh.read(2048)
    except OSError:
        return None
    import re

    m = re.search(r"\b(\d{2}/\d{2}/\d{4}|\d{4}-\d{2}-\d{2})\b", head)
    return m.group(1) if m else None


def _full_name(row: list[str], name_cols: list[int]) -> str:
    parts = [_cell(row, c) for c in name_cols]
    return " ".join(p for p in parts if p)


def _parse_csv(path: Path) -> list[UkEntry]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []

    header_idx = 0
    for i, r in enumerate(rows[:20]):
        if any("unique id" in (c or "").lower() for c in r):
            header_idx = i
            break
    cols = _resolve_columns(rows[header_idx])
    uid_col = cols["uid"] if cols["uid"] is not None else cols["ofsi_group"]
    if uid_col is None or not cols["name_cols"]:
        logger.warning("UK CSV: could not resolve Unique ID / Name columns; header=%r", rows[header_idx])
        return []

    grouped: dict[str, dict] = {}
    for r in rows[header_idx + 1:]:
        if not any((c or "").strip() for c in r):
            continue
        uid_raw = _cell(r, uid_col)
        if not uid_raw:
            continue
        g = grouped.setdefault(
            uid_raw,
            {
                "primary": None,
                "primary_native": None,
                "aliases": [],
                "aliases_native": [],
                "programs": set(),
                "countries": [],
                "addresses": [],
                "type": "Unknown",
                "list_date": None,
            },
        )

        name = _full_name(r, cols["name_cols"])
        name_type = _cell(r, cols["name_type"]).lower()
        is_primary = "primary" in name_type
        quality = "Weak" if "weak" in name_type else "Strong"

        if name:
            if is_primary and g["primary"] is None:
                g["primary"] = name
            elif name != g["primary"]:
                g["aliases"].append((name, quality))

        nonlatin = _cell(r, cols["nonlatin"])
        if nonlatin:
            script = _detect_script(nonlatin)
            if g["primary_native"] is None and is_primary:
                g["primary_native"] = nonlatin
            g["aliases_native"].append((nonlatin, quality, script))

        t = _map_type(_cell(r, cols["type"]))
        if t != "Unknown":
            g["type"] = t

        regime = _cell(r, cols["regime"])
        if regime:
            g["programs"].add(regime)

        country = _cell(r, cols["country"])
        if country and country not in g["countries"]:
            g["countries"].append(country)
            g["addresses"].append({"city": "", "state": "", "postal": "", "country": country})

        if g["list_date"] is None:
            d = _cell(r, cols["designated"])
            if d:
                g["list_date"] = d

    out: list[UkEntry] = []
    for uid_raw, g in grouped.items():
        primary = g["primary"] or (g["aliases"][0][0] if g["aliases"] else (g["primary_native"] or ""))
        if not primary and not g["primary_native"]:
            continue
        seen: set[str] = set()
        aliases: list[tuple[str, str]] = []
        for nm, q in g["aliases"]:
            key = nm.casefold()
            if not nm or nm == primary or key in seen:
                continue
            seen.add(key)
            aliases.append((nm, q))
        out.append(
            UkEntry(
                uid=_stable_uid(uid_raw),
                primary_name=primary,
                sdn_type=g["type"],
                programs=tuple(sorted(g["programs"])),
                aliases=tuple(aliases),
                address_countries=tuple(g["countries"]),
                list_date=g["list_date"],
                primary_name_native=g["primary_native"],
                aliases_native=tuple(g["aliases_native"]),
                addresses=tuple(g["addresses"]),
                uk_unique_id=uid_raw,
            )
        )
    return out


@functools.lru_cache(maxsize=2)
def _load_uk_entries_cached(mtime_ns: int) -> tuple[UkEntry, ...]:
    _ = mtime_ns  # cache key only
    return tuple(_parse_csv(_csv_path()))


def load_uk_entries() -> list[UkEntry]:
    """Return parsed UK Sanctions List entries, or ``[]`` if not downloaded."""
    path = _csv_path()
    if not path.exists():
        return []
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return []
    try:
        return list(_load_uk_entries_cached(mtime_ns))
    except Exception as exc:  # noqa: BLE001
        logger.exception("failed to parse UK Sanctions List CSV: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def uk_status() -> dict:
    """Describe the on-disk UK CSV for the frontend freshness widget."""
    path = _csv_path()
    marker = _read_marker()
    base = {
        "list_version": marker.get("list_version"),
        "fetched_at": marker.get("fetched_at"),
        "last_error": marker.get("error"),
    }
    if not path.exists():
        return {**base, "available": False, "entry_count": 0, "bytes": 0, "age_seconds": None}
    try:
        st = path.stat()
    except OSError:
        return {**base, "available": False, "entry_count": 0, "bytes": 0, "age_seconds": None}
    try:
        entry_count = len(load_uk_entries())
    except Exception:  # noqa: BLE001
        entry_count = 0
    return {
        **base,
        "available": True,
        "entry_count": entry_count,
        "bytes": st.st_size,
        "age_seconds": int(time.time() - st.st_mtime),
    }
