"""Pydantic API models for the HITL Invoice reviewer.

Re-exports the existing `Invoice` / `LineItem` / `BankInfo` schema from
`extract.py` unchanged, and adds the wrapper / link / patch models the
frontend and worker consume.

All field names and types here are the source-of-truth for the HTTP API and
must match `app/contract.md` exactly.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, model_validator

# Re-export the raw invoice schema from the existing extractor module.
# These are the same classes used by `extract.extract()` as the LLM
# `response_format`, so the structure is guaranteed to round-trip.
from extract import BankInfo, Invoice, LineItem

__all__ = [
    "ApproveRequest",
    "BankInfo",
    "FieldLink",
    "FieldPatch",
    "Invoice",
    "JobDetail",
    "JobResult",
    "JobStatus",
    "JobSummary",
    "LineItem",
    "LineItemCreate",
    "LineItemLink",
    "LineItemPatch",
    "OcrPageTokens",
    "OcrPayload",
    "OcrToken",
    "PageInfo",
    "PageROIs",
    "Polygon",
    "Rect",
    "SanctionsMatch",
    "SanctionsStatus",
    "SanctionsVerdict",
    "StartJobRequest",
]


# Sanctions screening verdict — server-derived metadata attached to a job.
# Not part of `Invoice` so it never round-trips through the LLM or approved.json.
SanctionsStatus = Literal["green", "yellow", "red", "not_checked", "error"]
SanctionsRole = Literal[
    "seller",
    "bank",
    "consignee",
    "customer",
    "beneficiary",
    "payable_to",
]

# LLM judge verdict per candidate (stage 2 of screening):
#   confirmed → LLM thinks the invoice party IS the OFAC entity.
#   rejected  → LLM thinks they are different (false positive).
#   unclear   → LLM cannot decide from available context.
#   skipped   → fuzzy score was high enough (>= RAPIDFUZZ_AUTO_CONFIRM) that
#               we treated the match as confirmed without an LLM call.
#   error     → LLM call failed; treat as unclear for downstream status logic.
LlmVerdict = Literal["confirmed", "rejected", "unclear", "skipped", "error"]


class SanctionsMatch(BaseModel):
    """One OFAC SDN entry that fuzzy-matched an invoice party."""

    role: SanctionsRole
    uid: int
    primary_name: str
    matched_name: str  # the specific alias or primary that scored highest
    sdn_type: str  # "Entity" | "Individual" | "Vessel" | "Aircraft" | "Unknown"
    programs: list[str]
    score: float  # 0..100 (rapidfuzz, stage 1)
    address_countries: list[str]
    treasury_url: str
    # Source list — "SDN" | "CONSOLIDATED" | "CSL_BIS_ENTITY" | etc.
    # Defaults to "SDN" so legacy rows w/o this field deserialize cleanly.
    source_list: str = "SDN"
    # Which alias bucket scored highest. Lets the LLM judge see if the match
    # came from a cross-script alias vs the Latin primary name.
    matched_script: str | None = None
    # Full address rows from the source entry (street, city, country, ...).
    # Surfaced so the LLM judge has the same location context a human would.
    addresses: list[dict] | None = None
    # Stage 2 (LLM judge) fields — present after the LLM has reviewed the
    # candidate. Optional so legacy rows without these still validate.
    llm_verdict: LlmVerdict | None = None
    llm_reasoning: str | None = None


class SanctionsVerdict(BaseModel):
    """Aggregated sanctions-screening result for a job."""

    status: SanctionsStatus
    checked_at: str | None  # ISO 8601
    list_version: str | None  # OFAC list_date (e.g. "2026-05-22")
    error: str | None = None
    matches: list[SanctionsMatch] = []
    # LLM judge metadata. Null if LLM stage was skipped (e.g. no candidates,
    # or env var disabled the stage).
    llm_model: str | None = None
    llm_error: str | None = None


# A polygon is a list of 4 [x, y] integer points in source-image pixel coords.
# Kept as a plain alias so JSON serialization stays as a nested list.
Polygon = list[list[int]]


class JobStatus(str, Enum):
    """FSM states the worker progresses through. See contract.md."""

    CREATED = "created"   # pages rendered, awaiting user ROI selection + Start
    QUEUED = "queued"
    OCR = "ocr"
    EXTRACT = "extract"
    LINKING = "linking"
    READY = "ready"
    APPROVED = "approved"
    FAILED = "failed"


class PageInfo(BaseModel):
    """One rendered PDF page (used by the frontend to size SVG overlays)."""

    page: int  # 1-indexed
    image_url: str  # e.g. "/api/jobs/<id>/page/1.png"
    width: int  # source-image pixel width
    height: int  # source-image pixel height


class FieldLink(BaseModel):
    """One leaf field from `Invoice`, dot-pathed, linked to an OCR bbox."""

    field_path: str  # e.g. "invoice_number", "bank.account_number", "remarks[0]"
    value: str | int | float | bool | None
    page: int | None  # 1-indexed; null if unlinked
    bbox: Polygon | None  # null if unlinked or no match
    score: float  # rapidfuzz score 0-100; 0 if unlinked
    edited: bool = False  # true after user PATCH


class LineItemLink(BaseModel):
    """One line-item row from `Invoice.line_items`, linked to its row bbox."""

    row_index: int  # 0-based
    data: LineItem  # full nested object
    page: int | None
    bbox: Polygon | None  # bounding rect spanning the whole row
    edited: bool = False


class JobSummary(BaseModel):
    """Sidebar listing entry."""

    id: str
    source_filename: str
    page_count: int | None
    status: JobStatus
    progress_pct: int  # 0..100
    created_at: str  # ISO 8601


class JobResult(BaseModel):
    """Populated when `status` is ready or approved.

    `kind` discriminates the three extraction paths:
      * "invoice"     — structured Invoice + field/line-item links.
      * "prompt_json" — free-form prompt run, JSON output (raw inferred dict).
      * "prompt_text" — free-form prompt run, plain-text output.
    """

    kind: Literal["invoice", "prompt_json", "prompt_text"]
    invoice: Invoice | None = None       # kind == "invoice"
    data: dict | None = None             # kind == "prompt_json" (raw inferred dict)
    text: str | None = None              # kind == "prompt_text"
    fields: list[FieldLink] = []         # invoice + prompt_json
    line_items: list[LineItemLink] = []  # invoice only
    pages: list[PageInfo] = []           # all kinds


class JobDetail(BaseModel):
    """Full job document returned by `GET /api/jobs/{id}`."""

    id: str
    source_filename: str
    page_count: int | None
    status: JobStatus
    progress_pct: int
    mode: Literal["invoice", "prompt"] = "invoice"
    output_format: Literal["text", "json"] | None = None
    error: str | None = None
    created_at: str
    updated_at: str
    result: JobResult | None = None


class ApproveRequest(BaseModel):
    """`POST /api/jobs/{id}/approve` body.

    Both fields are optional unless the sanctions verdict is "red", in which
    case the backend requires `sanctions_override=True` and a non-empty
    `override_reason` (>= 10 chars) before the job can flip to approved.
    """

    sanctions_override: bool = False
    override_reason: str | None = None


# --- Request payloads ---------------------------------------------------------


class FieldPatch(BaseModel):
    """`PATCH /api/jobs/{id}/fields/{field_path}` body."""

    value: str | int | float | None = None
    page: int | None = None
    bbox: Polygon | None = None


class LineItemCreate(BaseModel):
    """`POST /api/jobs/{id}/line-items` body."""

    data: LineItem
    page: int | None = None
    bbox: Polygon | None = None


class LineItemPatch(BaseModel):
    """`PATCH /api/jobs/{id}/line-items/{row_index}` body."""

    data: LineItem | None = None
    page: int | None = None
    bbox: Polygon | None = None


# --- OCR token payload (reverse-lookup: PDF click → field) --------------------


class OcrToken(BaseModel):
    text: str
    bbox: Polygon
    score: float


class OcrPageTokens(BaseModel):
    page: int
    width: int
    height: int
    tokens: list[OcrToken]


class OcrPayload(BaseModel):
    pages: list[OcrPageTokens]


# --- ROI (region-of-interest) selection -------------------------------------


class Rect(BaseModel):
    """Axis-aligned rectangle in source-image pixel coordinates."""

    x: int
    y: int
    w: int
    h: int


class PageROIs(BaseModel):
    """All rects the user drew on a single page. Empty rects[] = full page."""

    page: int  # 1-indexed
    rects: list[Rect]


class StartJobRequest(BaseModel):
    """POST /api/jobs/{id}/start body.

    Pages absent from `rois` or with an empty `rects` list are OCR'd in full.
    `language` selects the OCR recognizer: "en" (English) or "fa" (Persian).

    `mode` picks the extraction path:
      * "invoice" (default) — structured Invoice extraction; `prompt`/
        `output_format` are ignored.
      * "prompt"  — free-form run driven by `prompt`; `output_format` selects
        text vs JSON output. Both are required in this mode.
    """

    rois: list[PageROIs]
    language: Literal["en", "fa"] = "en"
    mode: Literal["prompt", "invoice"] = "invoice"
    prompt: str | None = None
    output_format: Literal["text", "json"] | None = None

    @model_validator(mode="after")
    def _validate_prompt_mode(self) -> "StartJobRequest":
        # Prompt mode is meaningless without an instruction + a chosen output
        # shape; reject early so FastAPI surfaces a 422 rather than the worker
        # failing mid-run. Invoice mode ignores prompt/output_format entirely.
        if self.mode == "prompt":
            if not (self.prompt and self.prompt.strip()):
                raise ValueError("prompt is required when mode == 'prompt'")
            if self.output_format not in {"text", "json"}:
                raise ValueError(
                    "output_format must be 'text' or 'json' when mode == 'prompt'"
                )
        return self
