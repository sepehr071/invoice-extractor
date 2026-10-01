"""Background worker: PDF → OCR → LLM extract → field/bbox linking → SQLite.

Invoked synchronously from FastAPI via `BackgroundTasks.add_task(run_job, job_id)`.
Mirrors the orchestration in `process.py`, but instead of just dumping JSON
to disk it updates the SQLite `jobs` / `fields` / `line_items` tables so the
HITL frontend can read progress and results.

Status FSM (see contract.md):
    queued → ocr → extract → linking → ready
                                     └─→ failed
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# OCR confidence threshold (tokens with score < this are dropped before LLM).
# Override per deployment via `OCR_MIN_SCORE` env var. Range [0.0, 1.0].
_OCR_MIN_SCORE = float(os.environ.get("OCR_MIN_SCORE", "0.5"))

from PIL import Image

# Existing pipeline modules — imported unchanged.
from extract import extract, extract_freeform, ocr_to_text
from pdf_ocr import ocr_page, pdf_to_images

# Backend helpers.
from app.db import (
    add_line_item,
    audit_log,
    get_conn,
    get_job_language,
    get_job_mode,
    get_job_output_format,
    get_job_prompt,
    update_job_status,
    upsert_field_link,
)
from app.linking import link_dict_to_bboxes, link_fields_to_bboxes
from app.models import Invoice
from app.storage import (
    find_job_source,
    job_dir,
    job_extracted_json,
    job_ocr_json,
    job_page_image,
    job_page_masked_image,
    job_pages_dir,
    job_pages_masked_dir,
    job_result_text,
    job_rois_json,
)

__all__ = ["run_job"]


# ---------------------------------------------------------------------------
# Progress helpers
# ---------------------------------------------------------------------------

# Progress percentages per phase, matching contract.md heuristics.
_OCR_START_PCT = 10
_OCR_END_PCT = 70
_EXTRACT_PCT = 75
_LINKING_PCT = 88
_DONE_PCT = 100


def _ocr_progress(done: int, total: int) -> int:
    """Linearly map (done/total) → 10..70."""
    if total <= 0:
        return _OCR_START_PCT
    span = _OCR_END_PCT - _OCR_START_PCT
    return _OCR_START_PCT + int(round(span * (done / total)))


# ---------------------------------------------------------------------------
# Phase implementations
# ---------------------------------------------------------------------------


# Job language ("en" | "fa") → PaddleOCR recognizer language. Use PaddleOCR's
# documented ISO token "fa": in PaddleOCR 3.x it loads the PP-OCRv5 Arabic-script
# recognizer (arabic_PP-OCRv5_mobile_rec), whose supported languages explicitly
# include Persian (Persian-specific letters پ چ ژ گ + Persian digits). The old
# 2.x script-family alias "arabic" is NOT a valid 3.x token and silently fails to
# load any recognizer → empty OCR. Unknown languages fall back to English.
_OCR_LANG_MAP: dict[str, str] = {
    "en": "en",
    "fa": "fa",
}

# Cache one PaddleOCR instance per resolved recognizer language. Building a
# PaddleOCR engine loads model weights onto the GPU, so we never want two
# instances for the same language alive at once.
_OCR_ENGINES: dict[str, object] = {}


def _resolve_ocr_lang(job_lang: str | None) -> str:
    """Map a job language to a PaddleOCR recognizer language."""
    return _OCR_LANG_MAP.get((job_lang or "en").lower(), "en")


def make_ocr_engine(lang: str):
    """Return a PaddleOCR (PP-OCRv5) engine for `lang`, cached per resolved lang.

    `lang` is the job language ("en" | "fa"); it is resolved to the recognizer
    language via `_OCR_LANG_MAP` before construction. The import is kept inside
    the function so importing this module (e.g. from tests or the API process
    without a GPU) does not pay the PaddleOCR import / GPU-init cost.
    """
    resolved = _resolve_ocr_lang(lang)
    engine = _OCR_ENGINES.get(resolved)
    if engine is None:
        from paddleocr import PaddleOCR

        engine = PaddleOCR(
            device="gpu:0",
            ocr_version="PP-OCRv5",
            lang=resolved,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
        _OCR_ENGINES[resolved] = engine
    return engine


def _read_image_size(path: Path) -> tuple[int, int]:
    """Return (width, height) for a PNG. Uses PIL, lazy-closed."""
    with Image.open(path) as img:
        return int(img.width), int(img.height)


def _load_rois(job_id: str) -> dict[int, list[dict]]:
    """Read rois.json into a {page_number: [rect_dict, ...]} map.

    Returns empty dict when no rois file or no rects drawn.
    """
    path = job_rois_json(job_id)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    out: dict[int, list[dict]] = {}
    for entry in data.get("rois", []) or []:
        page = int(entry.get("page", 0))
        rects = entry.get("rects") or []
        if page > 0 and rects:
            out[page] = rects
    return out


def _build_masked_page(original_path: Path, rects: list[dict], out_path: Path) -> None:
    """Create a white-background image with only the user-selected rects pasted in.

    `rects` are source-image-pixel boxes `{x, y, w, h}`. Areas outside the rect
    union appear as solid white in the output PNG.
    """
    with Image.open(original_path) as src:
        src = src.convert("RGB")
        w, h = src.size
        canvas = Image.new("RGB", (w, h), "white")
        for r in rects:
            x = max(0, int(r.get("x", 0)))
            y = max(0, int(r.get("y", 0)))
            rw = max(0, int(r.get("w", 0)))
            rh = max(0, int(r.get("h", 0)))
            if rw <= 0 or rh <= 0:
                continue
            x2 = min(w, x + rw)
            y2 = min(h, y + rh)
            if x2 <= x or y2 <= y:
                continue
            crop = src.crop((x, y, x2, y2))
            canvas.paste(crop, (x, y))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, "PNG")


def _read_job_row(job_id: str) -> dict:
    """Fetch the jobs row as a dict (for source_filename, etc.)."""
    conn = get_conn()
    try:
        cursor = conn.execute(
            "SELECT id, source_filename, status FROM jobs WHERE id = ?",
            (job_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(f"job {job_id!r} not found in jobs table")
        # sqlite3.Row supports indexing by column name when row_factory is set.
        try:
            return {key: row[key] for key in row.keys()}
        except AttributeError:
            return {
                "id": row[0],
                "source_filename": row[1],
                "status": row[2],
            }
    finally:
        conn.close()


def _run_ocr_phase(job_id: str, source_path: Path) -> tuple[dict, list[Path]]:
    """Run PaddleOCR on every already-rendered page, persist `ocr.json`.

    Pages are rendered/normalized synchronously at upload time (PDFs via
    `pdf_to_images`, single images via `image_to_page`), so this phase expects
    `output/<job_id>/pages/page_NNN.png` to exist. If a page has user-drawn
    ROI rects (from `rois.json`), the page is first composited onto a white
    canvas with only the rect interiors retained, and OCR runs on that
    masked PNG. Pages with no rects are OCR'd as-is. Coordinates returned
    by PaddleOCR remain in original page-pixel space either way.

    Returns:
        merged: the `ocr.json` dict (same shape as `pdf_ocr.main()` writes).
        image_paths: list of original page PNG paths, 1-indexed order.
    """
    update_job_status(job_id, "ocr", _OCR_START_PCT)

    pages_dir = job_pages_dir(job_id)

    # Discover existing rendered pages, ascending. Fall back to re-rendering
    # only when the directory is missing/empty (CLI / legacy callers) and the
    # source is a PDF; image jobs are normalized to page_001.png at upload.
    image_paths = sorted(pages_dir.glob("page_*.png")) if pages_dir.exists() else []
    if not image_paths:
        if source_path.suffix.lower() == ".pdf":
            image_paths = pdf_to_images(source_path, pages_dir, dpi=400)
        else:
            raise RuntimeError(f"no rendered pages for job {job_id!r}")
    total = len(image_paths)
    if total == 0:
        raise RuntimeError(f"no pages found for job {job_id!r}")

    rois = _load_rois(job_id)
    masked_dir = job_pages_masked_dir(job_id)

    job_lang = get_job_language(job_id)
    ocr = make_ocr_engine(job_lang)

    merged: dict = {
        "source": str(source_path),
        "lang": job_lang,
        "dpi": 400,
        "page_count": total,
        "pages": [],
    }

    job_root = job_dir(job_id)
    for i, img_path in enumerate(image_paths, start=1):
        page_rects = rois.get(i)
        ocr_input = img_path
        if page_rects:
            masked_path = job_page_masked_image(job_id, i)
            _build_masked_page(img_path, page_rects, masked_path)
            ocr_input = masked_path

        page_result = ocr_page(ocr, ocr_input)
        page_result["page"] = i
        try:
            page_result["image"] = str(img_path.relative_to(job_root))
        except ValueError:
            page_result["image"] = img_path.name

        # Always record dimensions from the ORIGINAL page so the frontend
        # overlay coordinate system matches what the user sees.
        width, height = _read_image_size(img_path)
        page_result["width"] = width
        page_result["height"] = height
        if page_rects:
            page_result["rois"] = page_rects

        merged["pages"].append(page_result)
        update_job_status(job_id, "ocr", _ocr_progress(i, total))

    ocr_path = job_ocr_json(job_id)
    ocr_path.parent.mkdir(parents=True, exist_ok=True)
    ocr_path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    # Silence unused-binding when no rois were drawn this job.
    _ = masked_dir
    return merged, image_paths


def _run_extract_phase(job_id: str, ocr_data: dict) -> Invoice:
    """Flatten OCR → LLM → write `extracted.json` → return parsed `Invoice`."""
    update_job_status(job_id, "extract", _EXTRACT_PCT)

    text = ocr_to_text(ocr_data, min_score=_OCR_MIN_SCORE)
    extracted_dict = extract(text)

    extracted_path = job_extracted_json(job_id)
    extracted_path.parent.mkdir(parents=True, exist_ok=True)
    extracted_path.write_text(
        json.dumps(extracted_dict, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return Invoice.model_validate(extracted_dict)


def _run_freeform_phase(
    job_id: str, ocr_data: dict, prompt: str, output_format: str
) -> str | dict:
    """Prompt-driven extraction (no fixed schema). Persists the result.

    `json` output is written to `extracted.json` (so the read path reuses the
    same artifact as invoice mode); `text` output is written to `result.txt`.
    Returns the raw result (dict for json, str for text).
    """
    update_job_status(job_id, "extract", _EXTRACT_PCT)

    text = ocr_to_text(ocr_data, min_score=_OCR_MIN_SCORE)
    fmt = "json" if output_format == "json" else "text"
    result = extract_freeform(text, prompt, fmt)

    if fmt == "json":
        extracted_path = job_extracted_json(job_id)
        extracted_path.parent.mkdir(parents=True, exist_ok=True)
        extracted_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    else:
        result_path = job_result_text(job_id)
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            result if isinstance(result, str) else str(result),
            encoding="utf-8",
        )

    return result


def _run_generic_linking_phase(
    job_id: str, result: dict, ocr_data: dict, page_count: int
) -> None:
    """Best-effort field→bbox linking for prompt/json results (no line items)."""
    update_job_status(job_id, "linking", _LINKING_PCT, page_count=page_count)

    field_links = link_dict_to_bboxes(result, ocr_data.get("pages", []))
    for link in field_links:
        upsert_field_link(
            job_id,
            link.field_path,
            link.value,
            link.page,
            link.bbox,
            link.score,
            link.edited,
        )


def _run_linking_phase(
    job_id: str, invoice: Invoice, ocr_data: dict, page_count: int
) -> None:
    """Fuzzy-match each leaf + line-item row to OCR tokens, persist results."""
    update_job_status(job_id, "linking", _LINKING_PCT, page_count=page_count)

    field_links, line_item_links = link_fields_to_bboxes(invoice, ocr_data.get("pages", []))

    for link in field_links:
        upsert_field_link(
            job_id,
            link.field_path,
            link.value,
            link.page,
            link.bbox,
            link.score,
            link.edited,
        )

    for link in line_item_links:
        add_line_item(
            job_id,
            link.data.model_dump(),
            link.page,
            link.bbox,
            link.edited,
            link.row_index,
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_job(job_id: str) -> None:
    """Synchronous worker entrypoint, called from FastAPI BackgroundTasks.

    On any unhandled exception the job is moved to status `failed` with the
    error message recorded, then the exception is re-raised so FastAPI logs it.
    """
    try:
        _read_job_row(job_id)  # sanity check: row must exist
        source_path = find_job_source(job_id)
        if source_path is None or not source_path.exists():
            raise FileNotFoundError(f"source file missing for job {job_id!r}")

        # 1) OCR (mode-agnostic)
        ocr_data, image_paths = _run_ocr_phase(job_id, source_path)
        page_count = len(image_paths)

        # Cheap audit so the timeline is reconstructible later.
        try:
            audit_log(job_id, "ocr_done", {"page_count": page_count})
        except Exception:
            # Audit log failures should never kill a job.
            pass

        mode = get_job_mode(job_id)

        if mode == "prompt":
            # 2/3) Prompt-driven extract (+ best-effort linking for json output)
            prompt = get_job_prompt(job_id) or ""
            output_format = get_job_output_format(job_id) or "json"
            result = _run_freeform_phase(job_id, ocr_data, prompt, output_format)
            if output_format == "json" and isinstance(result, dict):
                _run_generic_linking_phase(job_id, result, ocr_data, page_count=page_count)
            try:
                audit_log(job_id, "extract_done", {"mode": "prompt", "output_format": output_format})
            except Exception:
                pass
        else:
            # 2) Extract (typed invoice schema)
            invoice = _run_extract_phase(job_id, ocr_data)
            try:
                audit_log(job_id, "extract_done", {"mode": "invoice", "line_items": len(invoice.line_items)})
            except Exception:
                pass

            # 3) Linking + persist
            _run_linking_phase(job_id, invoice, ocr_data, page_count=page_count)
            try:
                audit_log(job_id, "linking_done", {})
            except Exception:
                pass

        # 4) Ready
        update_job_status(job_id, "ready", _DONE_PCT, page_count=page_count)

    except Exception as exc:  # noqa: BLE001  — broad catch is intentional
        try:
            update_job_status(job_id, "failed", error=str(exc))
        except Exception:
            # If even the status update fails, swallow it — original
            # exception is more useful for the caller.
            pass
        raise


# Silence unused-import warnings for helpers we expose via re-export only.
_ = (job_page_image,)
