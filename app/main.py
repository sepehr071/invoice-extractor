"""FastAPI entrypoint for the invoice HITL reviewer.

All routes prefixed under ``/api``. Background processing is started via
``BackgroundTasks`` — the long-running worker lives in ``app.jobs`` and is
written by Agent B.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import (
    BackgroundTasks,
    Body,
    FastAPI,
    File,
    HTTPException,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from pydantic import ValidationError
from PIL import Image

from app import db, storage
from app.jobs import run_job
from app.models import (
    ApproveRequest,
    FieldLink,
    FieldPatch,
    JobDetail,
    JobResult,
    JobStatus,
    LineItemCreate,
    LineItemLink,
    LineItemPatch,
    JobSummary,
    OcrPageTokens,
    OcrPayload,
    OcrToken,
    PageInfo,
    StartJobRequest,
)
from extract import Invoice
from pdf_ocr import image_to_page, pdf_to_images

app = FastAPI(title="invoice-ai HITL reviewer", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    storage.ensure_dirs()
    db.init_db()


# ---------- helpers ------------------------------------------------------

_JOB_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "25")) * 1024 * 1024
# magic-byte kind → stored source extension
_IMAGE_EXTS = {"jpeg": "jpg", "png": "png", "webp": "webp", "bmp": "bmp"}


def _require_job(job_id: str) -> dict:
    # job_id is a server-generated UUID; reject any other shape so a crafted id
    # can never escape OUTPUT_ROOT via the storage path joins (path traversal).
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"job {job_id} not found")
    return job


def _sniff_kind(data: bytes) -> str | None:
    """Detect file type by magic bytes — extension/content-type are untrusted."""
    if data[:4] == b"%PDF":
        return "pdf"
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    return None


def _page_size(job_id: str, page: int) -> tuple[int, int]:
    """Return (width, height) of the rendered page PNG."""
    img_path = storage.job_page_image(job_id, page)
    with Image.open(img_path) as im:
        return im.size  # (w, h)


def _build_job_detail(job_row: dict) -> JobDetail:
    job_id = job_row["id"]
    status = JobStatus(job_row["status"])
    mode = job_row.get("mode") or "invoice"
    output_format = job_row.get("output_format")

    result: JobResult | None = None
    if status in {JobStatus.READY, JobStatus.APPROVED}:
        # Pages (mode-agnostic).
        pages: list[PageInfo] = []
        page_count = job_row.get("page_count") or 0
        for i in range(1, (page_count or 0) + 1):
            img = storage.job_page_image(job_id, i)
            if not img.exists():
                continue
            w, h = _page_size(job_id, i)
            pages.append(
                PageInfo(
                    page=i,
                    image_url=f"/api/jobs/{job_id}/page/{i}.png",
                    width=w,
                    height=h,
                )
            )

        approved_path = storage.job_approved_json(job_id)
        extracted_path = storage.job_extracted_json(job_id)
        src_path = approved_path if approved_path.exists() else extracted_path

        if mode == "prompt" and output_format == "text":
            text_path = storage.job_result_text(job_id)
            text = text_path.read_text(encoding="utf-8") if text_path.exists() else ""
            result = JobResult(kind="prompt_text", text=text, pages=pages)
        elif mode == "prompt":
            # json: the inferred dict is loaded AS-IS (no Invoice schema), plus
            # whatever leaves the linker matched to bboxes.
            data = json.loads(src_path.read_text(encoding="utf-8")) if src_path.exists() else {}
            fields = [FieldLink.model_validate(f) for f in db.list_fields(job_id)]
            result = JobResult(kind="prompt_json", data=data, fields=fields, pages=pages)
        else:
            # invoice: typed schema + line items.
            if src_path.exists():
                invoice = Invoice.model_validate(json.loads(src_path.read_text(encoding="utf-8")))
            else:
                invoice = Invoice()
            fields = [FieldLink.model_validate(f) for f in db.list_fields(job_id)]
            line_items = [LineItemLink.model_validate(li) for li in db.list_line_items(job_id)]
            result = JobResult(
                kind="invoice",
                invoice=invoice,
                fields=fields,
                line_items=line_items,
                pages=pages,
            )

    return JobDetail(
        id=job_row["id"],
        source_filename=job_row["source_filename"],
        page_count=job_row.get("page_count"),
        status=status,
        progress_pct=int(job_row.get("progress_pct") or 0),
        mode=mode,
        output_format=output_format,
        error=job_row.get("error"),
        created_at=job_row["created_at"],
        updated_at=job_row["updated_at"],
        result=result,
    )


# ---------- field-path overlay (approval) --------------------------------

_PATH_SEG = re.compile(r"([^.\[\]]+)(?:\[(\d+)\])?")


def _coerce(old: Any, new: str) -> Any:
    """Coerce an edited string value back to the original field's type.

    On a failed numeric parse, fall back to ``None`` rather than returning the
    raw unparseable string: a non-numeric string in a numeric field would
    otherwise be written into approved.json and 500 every subsequent read via
    ``Invoice.model_validate``. A blank string is likewise treated as ``None``
    (clearing the field). When ``old`` is ``None`` the target type is unknown
    here — the approve endpoint re-validates the whole invoice as a backstop.
    """
    if new is None or (isinstance(new, str) and new.strip() == ""):
        return None
    if isinstance(old, bool):
        return new.strip().lower() in {"true", "1", "yes"}
    if isinstance(old, int) and not isinstance(old, bool):
        try:
            return int(float(new))
        except (ValueError, TypeError):
            return None
    if isinstance(old, float):
        try:
            return float(new)
        except (ValueError, TypeError):
            return None
    return new


def _get_at_path(root: Any, path: str) -> Any:
    """Read the current value at a field_path; None if absent/malformed."""
    cur = root
    for name, idx in _PATH_SEG.findall(path):
        if not isinstance(cur, dict) or name not in cur:
            return None
        cur = cur[name]
        if idx != "":
            if not isinstance(cur, list) or int(idx) >= len(cur):
                return None
            cur = cur[int(idx)]
    return cur


def _set_at_path(root: dict, path: str, value: Any) -> None:
    """Set `value` into nested dict/list following a field_path like
    `bank.accounts[0].iban` / `extra_notes[0]` / `company_name`.

    Creates intermediate dicts/lists as needed; no-ops on a malformed path.
    """
    segs = _PATH_SEG.findall(path)
    if not segs:
        return
    cur: Any = root
    for i, (name, idx) in enumerate(segs):
        last = i == len(segs) - 1
        has_idx = idx != ""
        if not isinstance(cur, dict):
            return
        if last and not has_idx:
            cur[name] = value
            return
        if has_idx:
            lst = cur.get(name)
            if not isinstance(lst, list):
                lst = []
                cur[name] = lst
            ix = int(idx)
            while len(lst) <= ix:
                lst.append(None)
            if last:
                lst[ix] = value
                return
            if not isinstance(lst[ix], dict):
                lst[ix] = {}
            cur = lst[ix]
        else:
            nxt = cur.get(name)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[name] = nxt
            cur = nxt


def _overlay_edited_fields(invoice_dict: dict, fields: list[dict]) -> None:
    """Apply reviewer scalar-field edits (the `fields` table) onto the invoice.

    Without this, approved.json is built from raw extracted.json and every
    edit the reviewer made to a scalar/nested field (company_name,
    bank.swift_code, ...) is silently dropped — defeating the HITL workflow.
    Line items live in their own table and are applied separately, so
    `line_items[...]` paths are skipped here. Values are coerced back to the
    original field's type.
    """
    for f in fields:
        if not f.get("edited"):
            continue
        path = f.get("field_path") or ""
        if not path or path.startswith("line_items"):
            continue
        raw = f.get("value")
        old = _get_at_path(invoice_dict, path)
        _set_at_path(invoice_dict, path, _coerce(old, raw) if raw is not None else None)


# ---------- routes -------------------------------------------------------

@app.post("/api/jobs")
async def create_job_endpoint(
    file: UploadFile = File(...),
) -> dict:
    """Upload a PDF, render pages, leave the job in status 'created'.

    The OCR / LLM pipeline is NOT triggered here — the client navigates to the
    Preview page, optionally draws ROI rects per page, and then calls
    `POST /api/jobs/{id}/start` to enqueue the worker.
    """
    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit",
        )

    # Trust content, not the filename/extension: sniff magic bytes.
    kind = _sniff_kind(contents)
    if kind is None:
        raise HTTPException(
            status_code=400,
            detail="unsupported file; upload a PDF or image (JPG, PNG, WEBP, BMP)",
        )

    job_id = str(uuid.uuid4())
    storage.ensure_dirs()
    storage.job_dir(job_id).mkdir(parents=True, exist_ok=True)
    pages_dir = storage.job_pages_dir(job_id)
    pages_dir.mkdir(parents=True, exist_ok=True)

    ext = "pdf" if kind == "pdf" else _IMAGE_EXTS[kind]
    source_path = storage.job_source_path(job_id, ext)
    source_path.write_bytes(contents)

    # Render (PDF) or normalize (image) pages so Preview can fetch them
    # immediately. Offloaded to a threadpool — synchronous CPU work that would
    # otherwise block the single event loop and stall concurrent requests.
    try:
        if kind == "pdf":
            image_paths = await run_in_threadpool(pdf_to_images, source_path, pages_dir, 400)
        else:
            image_paths = await run_in_threadpool(image_to_page, source_path, pages_dir)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"failed to process upload: {exc}") from exc

    page_count = len(image_paths)
    db.create_job(job_id, file.filename or f"upload.{ext}", page_count=page_count)
    db.audit_log(
        job_id,
        "job_created",
        {"filename": file.filename, "kind": kind, "bytes": len(contents), "page_count": page_count},
    )

    return {"job_id": job_id, "page_count": page_count}


@app.post("/api/jobs/{job_id}/start", status_code=202)
def start_job_endpoint(
    job_id: str,
    body: StartJobRequest,
    background_tasks: BackgroundTasks,
) -> dict:
    """Persist user ROIs and enqueue the OCR + extract + linking worker.

    Rejects with 409 when the job is not in 'created' state.
    """
    job = _require_job(job_id)
    allowed = {JobStatus.CREATED.value, JobStatus.FAILED.value}
    if job["status"] not in allowed:
        raise HTTPException(
            status_code=409,
            detail=f"job status is {job['status']!r}; must be 'created' or 'failed' to start",
        )
    is_restart = job["status"] == JobStatus.FAILED.value

    page_count = int(job.get("page_count") or 0)

    # Validate roi shape: pages in range, rects positive.
    normalized: list[dict] = []
    for page_rois in body.rois:
        if page_count and not (1 <= page_rois.page <= page_count):
            raise HTTPException(
                status_code=400,
                detail=f"roi page {page_rois.page} out of range (1..{page_count})",
            )
        rects: list[dict] = []
        for r in page_rois.rects:
            if r.w <= 0 or r.h <= 0:
                continue  # skip degenerate
            rects.append({"x": int(r.x), "y": int(r.y), "w": int(r.w), "h": int(r.h)})
        if rects:
            normalized.append({"page": page_rois.page, "rects": rects})

    rois_path = storage.job_rois_json(job_id)
    rois_path.parent.mkdir(parents=True, exist_ok=True)
    rois_path.write_text(
        json.dumps({"rois": normalized}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if is_restart:
        # Clean slate: drop any partial field/line-item rows from the failed
        # run so re-linking can't collide on the UNIQUE(job_id, row_index)
        # constraint, and clear the stale error message.
        db.replace_fields(job_id, [])
        db.replace_line_items(job_id, [])
        with db.get_conn() as conn:
            conn.execute("UPDATE jobs SET error = NULL WHERE id = ?", (job_id,))

    # Persist OCR language + run mode before enqueuing so the worker reads them.
    db.set_job_language(job_id, body.language)
    db.set_job_mode(job_id, body.mode, body.prompt, body.output_format)

    db.update_job_status(job_id, JobStatus.QUEUED.value, progress_pct=0)
    db.audit_log(
        job_id,
        "job_restarted" if is_restart else "job_started",
        {
            "pages_with_rois": len(normalized),
            "language": body.language,
            "mode": body.mode,
            "output_format": body.output_format,
        },
    )

    background_tasks.add_task(run_job, job_id)
    return {"status": JobStatus.QUEUED.value}


@app.get("/api/jobs/{job_id}/rois")
def get_rois_endpoint(job_id: str) -> dict:
    """Return the user-drawn ROIs persisted at Start time (empty if none)."""
    _require_job(job_id)
    rois_path = storage.job_rois_json(job_id)
    if not rois_path.exists():
        return {"rois": []}
    try:
        return json.loads(rois_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"rois": []}


@app.get("/api/jobs", response_model=list[JobSummary])
def list_jobs_endpoint() -> list[JobSummary]:
    rows = db.list_jobs()
    return [
        JobSummary(
            id=r["id"],
            source_filename=r["source_filename"],
            page_count=r.get("page_count"),
            status=JobStatus(r["status"]),
            progress_pct=int(r.get("progress_pct") or 0),
            created_at=r["created_at"],
        )
        for r in rows
    ]


@app.get("/api/jobs/{job_id}", response_model=JobDetail)
def get_job_endpoint(job_id: str) -> JobDetail:
    job = _require_job(job_id)
    return _build_job_detail(job)


@app.get("/api/jobs/{job_id}/page/{n}.png")
def get_page_image(job_id: str, n: int) -> FileResponse:
    """Return the page PNG used by the review UI.

    If a ROI-masked version exists (generated during the OCR phase for pages
    the user cropped), it is served preferentially so the review pane shows
    only the region that was actually OCR'd. Otherwise the original page is
    returned. Both files share the same source-pixel dimensions, so frontend
    bbox overlays stay correct either way.
    """
    _require_job(job_id)
    masked = storage.job_page_masked_image(job_id, n)
    if masked.exists():
        path = masked
    else:
        path = storage.job_page_image(job_id, n)
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"page {n} not found")
    # `no-cache` (revalidate every request) is important: between Preview and
    # Review phases the file at this URL is swapped from `pages/` to
    # `pages_masked/`. Long-cached originals would otherwise mask the swap.
    return FileResponse(
        path=str(path),
        media_type="image/png",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.get("/api/jobs/{job_id}/ocr", response_model=OcrPayload)
def get_ocr(job_id: str) -> OcrPayload:
    _require_job(job_id)
    ocr_path = storage.job_ocr_json(job_id)
    if not ocr_path.exists():
        raise HTTPException(status_code=404, detail="job has not been OCR'd yet")

    raw = json.loads(ocr_path.read_text(encoding="utf-8"))
    pages_out: list[OcrPageTokens] = []
    for page in raw.get("pages", []):
        page_num = int(page.get("page", 0))
        texts = page.get("texts", []) or []
        boxes = page.get("boxes", []) or []
        scores = page.get("scores", []) or []

        width = page.get("width")
        height = page.get("height")
        if not width or not height:
            try:
                width, height = _page_size(job_id, page_num)
            except FileNotFoundError:
                width, height = 0, 0

        seen: set[tuple] = set()
        tokens: list[OcrToken] = []
        for i, text in enumerate(texts):
            if not text or not str(text).strip():
                continue
            if i >= len(boxes):
                continue
            bbox = boxes[i]
            score = float(scores[i]) if i < len(scores) else 0.0
            key = (str(text), tuple(tuple(pt) for pt in bbox))
            if key in seen:
                continue
            seen.add(key)
            tokens.append(OcrToken(text=str(text), bbox=bbox, score=score))

        pages_out.append(
            OcrPageTokens(
                page=page_num,
                width=int(width or 0),
                height=int(height or 0),
                tokens=tokens,
            )
        )
    return OcrPayload(pages=pages_out)


# ---------- field PATCH --------------------------------------------------

@app.patch("/api/jobs/{job_id}/fields/{field_path:path}", response_model=FieldLink)
def patch_field_endpoint(job_id: str, field_path: str, patch: FieldPatch) -> FieldLink:
    _require_job(job_id)
    before = db.get_field(job_id, field_path)
    if before is None:
        raise HTTPException(status_code=404, detail=f"field {field_path!r} not found")

    payload = patch.model_dump(exclude_unset=True)
    updated = db.patch_field(
        job_id,
        field_path,
        value=payload["value"] if "value" in payload else ...,
        page=payload["page"] if "page" in payload else ...,
        bbox=payload["bbox"] if "bbox" in payload else ...,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"field {field_path!r} not found")
    db.audit_log(job_id, "field_edit", {"path": field_path, "before": before, "after": updated})
    return FieldLink.model_validate(updated)


# ---------- line item CRUD ----------------------------------------------

@app.post("/api/jobs/{job_id}/line-items", response_model=LineItemLink)
def create_line_item_endpoint(job_id: str, body: LineItemCreate) -> LineItemLink:
    _require_job(job_id)
    item = db.add_line_item(
        job_id,
        data=body.data.model_dump(),
        page=body.page,
        bbox=body.bbox,
        edited=True,
    )
    db.audit_log(job_id, "line_add", item)
    return LineItemLink.model_validate(item)


@app.patch("/api/jobs/{job_id}/line-items/{row_index}", response_model=LineItemLink)
def patch_line_item_endpoint(job_id: str, row_index: int, body: LineItemPatch) -> LineItemLink:
    _require_job(job_id)
    before = db.get_line_item(job_id, row_index)
    if before is None:
        raise HTTPException(status_code=404, detail=f"line item {row_index} not found")

    payload = body.model_dump(exclude_unset=True)
    data_arg: object = ...
    if "data" in payload:
        data_arg = body.data.model_dump() if body.data is not None else None
    updated = db.patch_line_item(
        job_id,
        row_index,
        data=data_arg,  # type: ignore[arg-type]
        page=payload["page"] if "page" in payload else ...,
        bbox=payload["bbox"] if "bbox" in payload else ...,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"line item {row_index} not found")
    db.audit_log(job_id, "line_edit", {"before": before, "after": updated})
    return LineItemLink.model_validate(updated)


@app.delete("/api/jobs/{job_id}/line-items/{row_index}", status_code=204)
def delete_line_item_endpoint(job_id: str, row_index: int) -> Response:
    _require_job(job_id)
    before = db.get_line_item(job_id, row_index)
    if before is None:
        raise HTTPException(status_code=404, detail=f"line item {row_index} not found")
    if not db.delete_line_item(job_id, row_index):
        raise HTTPException(status_code=404, detail=f"line item {row_index} not found")
    db.audit_log(job_id, "line_delete", before)
    return Response(status_code=204)


# ---------- approval -----------------------------------------------------

@app.post("/api/jobs/{job_id}/approve")
def approve_job_endpoint(
    job_id: str,
    body: ApproveRequest | None = None,
) -> JSONResponse:
    job = _require_job(job_id)
    if job["status"] not in {JobStatus.READY.value, JobStatus.APPROVED.value}:
        raise HTTPException(
            status_code=409,
            detail=f"job status is {job['status']!r}; must be ready or approved",
        )
    _ = body  # ApproveRequest kept for request back-compat; no fields consumed now

    mode = job.get("mode") or "invoice"
    output_format = job.get("output_format")
    approved_path = storage.job_approved_json(job_id)
    approved_path.parent.mkdir(parents=True, exist_ok=True)

    # prompt + text: no editable fields/bbox — approval just finalizes status.
    if mode == "prompt" and output_format == "text":
        text_path = storage.job_result_text(job_id)
        text = text_path.read_text(encoding="utf-8") if text_path.exists() else ""
        db.update_job_status(job_id, JobStatus.APPROVED.value, progress_pct=100)
        db.audit_log(job_id, "approve", {"mode": "prompt", "output_format": "text"})
        return JSONResponse({"text": text})

    extracted_path = storage.job_extracted_json(job_id)
    base_dict: dict = {}
    if extracted_path.exists():
        base_dict = json.loads(extracted_path.read_text(encoding="utf-8"))

    # prompt + json: overlay reviewer scalar edits onto the inferred dict via the
    # generic dot-path overlay (no Invoice schema, arbitrary nesting).
    if mode == "prompt":
        _overlay_edited_fields(base_dict, db.list_fields(job_id))
        approved_path.write_text(
            json.dumps(base_dict, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        db.update_job_status(job_id, JobStatus.APPROVED.value, progress_pct=100)
        db.audit_log(
            job_id, "approve", {"mode": "prompt", "output_format": "json", "approved_path": str(approved_path)}
        )
        return JSONResponse({"result": base_dict, "approved_path": str(approved_path)})

    # invoice: typed rebuild — overlay edited fields, replace line items, then
    # re-validate before persisting so an invalid edit can't poison approved.json
    # (a non-numeric string in a numeric field would otherwise 500 every later read).
    invoice = Invoice.model_validate(base_dict or {})
    invoice_dict = invoice.model_dump()
    _overlay_edited_fields(invoice_dict, db.list_fields(job_id))
    invoice_dict["line_items"] = [li["data"] for li in db.list_line_items(job_id)]
    try:
        validated = Invoice.model_validate(invoice_dict)
    except ValidationError as exc:
        safe_errors = [
            {
                "field": ".".join(str(p) for p in e.get("loc", ())),
                "msg": e.get("msg"),
                "type": e.get("type"),
            }
            for e in exc.errors(include_url=False)
        ]
        raise HTTPException(
            status_code=422,
            detail={
                "message": "edited fields produce an invalid invoice; fix the flagged fields and retry",
                "errors": safe_errors,
            },
        ) from exc
    invoice_dict = validated.model_dump()
    approved_path.write_text(
        json.dumps(invoice_dict, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    db.update_job_status(job_id, JobStatus.APPROVED.value, progress_pct=100)
    db.audit_log(job_id, "approve", {"mode": "invoice", "approved_path": str(approved_path)})
    return JSONResponse({"invoice": invoice_dict, "approved_path": str(approved_path)})


@app.patch("/api/jobs/{job_id}/text")
def patch_text_endpoint(job_id: str, body: dict = Body(...)) -> dict:
    """Persist an edited prompt+text result back to result.txt."""
    job = _require_job(job_id)
    if (job.get("mode") or "invoice") != "prompt" or job.get("output_format") != "text":
        raise HTTPException(status_code=409, detail="job is not a prompt/text result")
    text = body.get("text")
    if not isinstance(text, str):
        raise HTTPException(status_code=422, detail="body must include a string 'text'")
    text_path = storage.job_result_text(job_id)
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text(text, encoding="utf-8")
    db.audit_log(job_id, "text_edited", {"chars": len(text)})
    return {"text": text}


# ---------- static SPA (built frontend) ----------------------------------
# Serve the Vite build when `frontend/dist` exists (production / same-origin).
# In dev the directory is absent, so nothing is mounted and the frontend runs
# from the Vite dev server on :5173 (allowed via CORS). All `/api/*` routes are
# registered above and therefore take precedence over the SPA fallback.

_FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


def _mount_frontend() -> None:
    """Mount the built SPA at '/' with an index.html fallback for client routes.

    No-op when the build directory is missing so dev startup is unaffected.
    """
    if not _FRONTEND_DIST.is_dir():
        return

    index_file = _FRONTEND_DIST / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> FileResponse:
        """Return a real static file when it exists, else the SPA shell.

        Known `/api/*` routes are registered first and take precedence; an
        *unknown* `/api/*` path must still 404 as JSON rather than be swallowed
        into the SPA shell, so it is rejected here. Any other path that maps to
        a file on disk (assets, favicon) is served directly; everything else
        falls back to index.html so the client-side router can take over.
        """
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="not found")
        candidate = (_FRONTEND_DIST / full_path).resolve()
        if (
            full_path
            and _FRONTEND_DIST in candidate.parents
            and candidate.is_file()
        ):
            return FileResponse(str(candidate))
        return FileResponse(str(index_file))


_mount_frontend()
