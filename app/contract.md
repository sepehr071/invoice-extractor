# Shared API Contract — General OCR + Extraction HITL UI

**This file is the source of truth.** If a field name or type differs anywhere, this file wins.

The app supports two **job modes**:
- **`invoice`** — typed `Invoice` extraction (schema below) + line items + bbox links.
- **`prompt`** — free-form prompt-driven extraction, no fixed schema. `output_format` selects `text` (markdown answer) or `json` (LLM-inferred dict → editable fields + best-effort bbox links).

The pipeline reuses these modules:
- `pdf_ocr.py` → `pdf_to_images()` (PDF → pages, with `MAX_PDF_PAGES`/`MAX_PAGE_MEGAPIXELS` guards), `image_to_page()` (single raster image → `pages/page_001.png`), `ocr_page()` returning `{"texts": list[str], "boxes": list[list[list[int]]], "scores": list[float]}` per page. `boxes[i]` is a 4-point polygon `[[x,y], [x,y], [x,y], [x,y]]` in source-image pixel coords.
- `extract.py` → `Invoice` Pydantic model (see exact schema below) + `extract(text, *, schema, system_prompt)` returns a `dict`; `extract_freeform(ocr_text, prompt, output_format)` returns a `dict` (json) or `str` (text). LLM endpoint/model resolved via `resolve_llm_config()` (`LLM_PROVIDER` profile or legacy flat vars).

Job artifacts live under `output/<job_id>/` and `output/<job_id>/pages/page_NNN.png`. The uploaded source is stored as `output/<job_id>/source.<ext>` (`pdf`/`jpg`/`png`/`webp`/`bmp`). Prompt+text results are stored as `output/<job_id>/result.txt`. Job IDs are UUIDv4.

> **No API authentication.** Every `/api/*` route is unauthenticated — a known gap, deliberately deferred (on-prem, LAN-only deployment). See `AUDIT.md`.

---

## Backend deps (added to `pyproject.toml`)

```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "python-multipart>=0.0.20",
    "rapidfuzz>=3.10",
    "openai>=1.50",
    "pydantic>=2.9",
    "python-dotenv>=1.0",
    "pypdfium2>=4.30",
    "paddleocr>=3.5",
    "inoutlists>=1.0.3",
]
```

`paddlepaddle` / `paddlepaddle-gpu` installed separately (already required by `pdf_ocr.py`).
`inoutlists` is retained for the dormant sanctions package only; nothing in the active pipeline uses it.

---

## Pydantic API models (`app/models.py`)

Re-exports `Invoice`, `LineItem`, `BankInfo` from `extract.py` (do not redefine). Adds:

```python
from enum import Enum
from typing import Literal
from pydantic import BaseModel
from extract import Invoice, LineItem, BankInfo  # re-export

# Polygon: 4 points [[x,y], [x,y], [x,y], [x,y]] in source-image pixel coords
Polygon = list[list[int]]


class JobStatus(str, Enum):
    CREATED = "created"   # pages rendered, awaiting user ROI selection + Start
    QUEUED = "queued"
    OCR = "ocr"
    EXTRACT = "extract"
    LINKING = "linking"
    READY = "ready"
    APPROVED = "approved"
    FAILED = "failed"


class Rect(BaseModel):
    x: int; y: int; w: int; h: int    # source-image pixel space


class PageROIs(BaseModel):
    page: int                          # 1-indexed
    rects: list[Rect]                  # empty == full page


class StartJobRequest(BaseModel):
    rois: list[PageROIs]
    language: Literal["en", "fa"] = "en"
    mode: Literal["invoice", "prompt"] = "invoice"
    prompt: str | None = None              # required (non-blank) when mode == "prompt"
    output_format: Literal["text", "json"] | None = None  # required when mode == "prompt"
    # model_validator: mode == "prompt" → prompt + output_format are required (422 otherwise)


class PageInfo(BaseModel):
    page: int                   # 1-indexed
    image_url: str              # e.g. "/api/jobs/<id>/page/1.png"
    width: int                  # source-image pixel width  (so frontend can scale SVG overlay)
    height: int                 # source-image pixel height


class FieldLink(BaseModel):
    """One leaf field from Invoice (dot path), linked to OCR bbox."""
    field_path: str             # e.g. "invoice_number", "bank.account_number"
    value: str | int | float | None
    page: int | None            # 1-indexed; null if unlinked
    bbox: Polygon | None        # null if unlinked or no match
    score: float                # rapidfuzz score 0-100; 0 if unlinked
    edited: bool = False        # true after user PATCH


class LineItemLink(BaseModel):
    """One line item row, linked to its OCR row bbox."""
    row_index: int              # 0-based
    data: LineItem              # full nested object (re-exported from extract.py)
    page: int | None
    bbox: Polygon | None        # bounding rect spanning the whole row
    edited: bool = False


class JobSummary(BaseModel):
    """Listing entry for sidebar."""
    id: str
    source_filename: str
    page_count: int | None
    status: JobStatus
    progress_pct: int           # 0..100
    created_at: str             # ISO 8601


class JobResult(BaseModel):
    """Returned when status == ready or approved. `kind` discriminates the
    three extraction paths; only the fields relevant to that kind are set."""
    kind: Literal["invoice", "prompt_json", "prompt_text"]
    invoice: Invoice | None = None        # kind == "invoice"
    data: dict | None = None              # kind == "prompt_json" (raw LLM-inferred dict, no schema)
    text: str | None = None               # kind == "prompt_text"
    fields: list[FieldLink] = []          # invoice + prompt_json (linked leaves)
    line_items: list[LineItemLink] = []   # invoice only
    pages: list[PageInfo] = []            # all kinds


class JobDetail(BaseModel):
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
    result: JobResult | None = None   # populated when status in {ready, approved}


class ApproveRequest(BaseModel):
    """POST /api/jobs/{id}/approve body. Kept for request back-compat; no
    fields are consumed by the endpoint anymore (approval is mode-aware)."""
    sanctions_override: bool = False
    override_reason: str | None = None


# --- Request payloads ---

class FieldPatch(BaseModel):
    """PATCH /api/jobs/{id}/fields/{field_path}"""
    value: str | int | float | None | None = None  # optional new value
    page: int | None = None                         # optional new page
    bbox: Polygon | None = None                     # optional new bbox (user clicked PDF)


class LineItemCreate(BaseModel):
    """POST /api/jobs/{id}/line-items"""
    data: LineItem
    page: int | None = None
    bbox: Polygon | None = None


class LineItemPatch(BaseModel):
    """PATCH /api/jobs/{id}/line-items/{row_index}"""
    data: LineItem | None = None
    page: int | None = None
    bbox: Polygon | None = None


# --- OCR tokens (for reverse-lookup: PDF click → field) ---

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
```

---

## Endpoint reference

All routes prefixed with `/api`. Content-type `application/json` unless noted.

### `POST /api/jobs`
- **Body:** multipart/form-data with field `file=<image or pdf>` — accepts PDF and single raster images (JPG/PNG/WEBP/BMP). Type is detected by **magic bytes**, not extension or content-type.
- **200:** `{ "job_id": "<uuid>", "page_count": <int> }`
- **400:** empty file, unsupported type (failed magic-byte sniff), or render/normalize failure.
- **413:** file exceeds the `MAX_UPLOAD_MB` cap (default 25 MB).
- **Side effect:** writes the upload to `output/<job_id>/source.<ext>` (ext derived from the sniffed kind), **renders (PDF) or normalizes (image) every page to `output/<job_id>/pages/page_NNN.png` synchronously** (off the event loop via a threadpool), inserts `jobs` row with `status=created`, `mode='invoice'`. Does NOT enqueue the worker — the client calls `POST /api/jobs/{id}/start` after picking mode/ROIs.

### `POST /api/jobs/{job_id}/start`
- **Body:** `StartJobRequest` — `{ "rois": [...], "language": "en"|"fa", "mode": "invoice"|"prompt", "prompt"?: str, "output_format"?: "text"|"json" }`. Pages absent from `rois` or with an empty `rects` list are OCR'd in full. `language` (default `"en"`) selects the OCR recognizer: `fa`→`arabic` (Persian), `en`→`en`. `mode` (default `"invoice"`) picks the extraction path; `prompt`/`output_format` apply only in prompt mode (ignored in invoice mode).
- **202:** `{ "status": "queued" }`
- **400:** rect references an out-of-range page.
- **409:** job is not in `created` or `failed` state (a `failed` job may be restarted; partial field/line-item rows are cleared first).
- **422:** `language` not in {`en`, `fa`}; `mode` not in {`invoice`, `prompt`}; or `mode == "prompt"` without a non-blank `prompt` **and** an `output_format` of `text`/`json`.
- **Side effect:** writes `output/<job_id>/rois.json`, persists `jobs.language` + `jobs.mode`/`jobs.prompt`/`jobs.output_format`, flips status to `queued`, enqueues `run_job`.

### `GET /api/jobs/{job_id}/rois`
- **200:** persisted `{ "rois": [...] }`. Returns `{ "rois": [] }` when no ROIs have been saved.

### `GET /api/jobs`
- **200:** `JobSummary[]`, newest first.

### `GET /api/jobs/{job_id}`
- **200:** `JobDetail` (carries `mode` + `output_format`). `result` is `null` until `status` ∈ {`ready`, `approved`}; once populated, `result.kind` reflects the mode/output_format combination:
  - `invoice` → `invoice` + `fields` + `line_items` + `pages`.
  - `prompt` + `json` → `prompt_json`: `data` (the raw LLM-inferred dict) + `fields` (generic dict→bbox links) + `pages`.
  - `prompt` + `text` → `prompt_text`: `text` + `pages` (no `fields`/linking).
- **404:** unknown job.

### `GET /api/jobs/{job_id}/page/{n}.png`
- `n` is 1-indexed.
- **200:** `image/png` body. Streams the file from `output/<job_id>/pages/page_NNN.png`.
- **404:** missing page.

### `GET /api/jobs/{job_id}/ocr`
- **200:** `OcrPayload`. Loaded from `output/<job_id>/ocr.json`. Tokens deduplicated and clean (no empty texts).
- **404:** if job not yet OCR'd.

### `PATCH /api/jobs/{job_id}/fields/{field_path}`
- `field_path` is URL-encoded dot path, e.g. `invoice_number` or `bank.account_number`.
- **Body:** `FieldPatch`
- **200:** updated `FieldLink`
- **Side effect:** marks `edited=true`, inserts `audit_log` row.

### `POST /api/jobs/{job_id}/line-items`
- **Body:** `LineItemCreate`
- **200:** new `LineItemLink` (server assigns `row_index` = current max + 1)

### `PATCH /api/jobs/{job_id}/line-items/{row_index}`
- **Body:** `LineItemPatch`
- **200:** updated `LineItemLink`

### `DELETE /api/jobs/{job_id}/line-items/{row_index}`
- **204:** deleted. Remaining `row_index` values are **not** re-numbered (gaps OK).

### `POST /api/jobs/{job_id}/approve`
- **Body (optional):** `ApproveRequest`. Accepted for back-compat but **no fields are consumed** — approval is mode-aware and has no override gate.
- **200:** finalized result, shaped by mode:
  - `invoice` → `{ "invoice": <Invoice JSON>, "approved_path": str }`.
  - `prompt` + `json` → `{ "result": <inferred dict>, "approved_path": str }`.
  - `prompt` + `text` → `{ "text": str }` (no `approved.json`).
- **409:** job status not in {`ready`, `approved`}.
- **422:** (invoice mode only) reviewer edits produce an invalid `Invoice`; body `{ "message": ..., "errors": [{field, msg, type}, ...] }`. The whole invoice is re-validated before persisting so a bad edit can't poison `approved.json`.
- **Side effect:** flips status to `approved`, audit-logs `approve`.
  - invoice → rebuilds `approved.json` from `extracted.json` with reviewer **scalar-field edits overlaid** (from the `fields` table) + line items replaced from the `line_items` table, then re-validated.
  - prompt+json → overlays reviewer scalar edits onto the inferred dict via the generic dot-path overlay (arbitrary nesting, no `Invoice` schema) → `approved.json`.
  - prompt+text → finalizes status only; the answer lives in `result.txt` (editable via `PATCH .../text`).

### `PATCH /api/jobs/{job_id}/text`
- **Body:** `{ "text": str }`.
- **200:** `{ "ok": true }`. Persists an edited prompt+text answer back to `output/<job_id>/result.txt`; audit-logs `text_edited`.
- **409:** job is not a `prompt`/`text` result.
- **422:** body missing a string `text`.

---

## SQLite schema (`app/db.py`)

Tables `jobs`, `fields`, `line_items`, `audit_log`. Use stdlib `sqlite3` with `check_same_thread=False`; no need for `aiosqlite` since FastAPI BackgroundTasks runs in threadpool.

The `jobs` table carries the run config:

```sql
CREATE TABLE jobs (
    id              TEXT PRIMARY KEY,
    source_filename TEXT NOT NULL,
    page_count      INTEGER,
    status          TEXT NOT NULL,
    progress_pct    INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    language        TEXT NOT NULL DEFAULT 'en',   -- "en" | "fa"
    mode            TEXT NOT NULL DEFAULT 'invoice', -- "invoice" | "prompt"
    prompt          TEXT,                          -- free-form instruction (prompt mode)
    output_format   TEXT,                          -- "text" | "json" (prompt mode)
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
```

`mode` / `prompt` / `output_format` are applied via additive `ALTER TABLE` migrations (guarded by `PRAGMA table_info`) for DBs that predate the general-OCR refactor; old rows read `mode='invoice'`.

`fields.bbox` and `line_items.bbox` store JSON-encoded `Polygon`. Decode on read. `get_conn()` opens with WAL + `busy_timeout=5000` so the background worker and request threadpool can write concurrently.

**`sanctions_results` (archived/dormant).** The table is still created by `init_db()` (and `app/db.py` retains its `upsert/get_sanctions_result` helpers), but sanctions screening is removed from the product — nothing in the request/worker path reads or writes it. Treat it as dead schema pending removal.

---

## Worker contract (`app/jobs.py` exports)

```python
def run_job(job_id: str) -> None:
    """Synchronous entrypoint for FastAPI BackgroundTasks.
    Reads jobs row, runs OCR then branches on mode, updates DB, writes artifacts.
    Updates status through queued → ocr → extract → linking → ready (or failed).
    """
```

After the (mode-agnostic) OCR phase, the worker branches on `jobs.mode`:
- **invoice:** `extract()` (typed `Invoice`) → `extracted.json` → `link_fields_to_bboxes()` (fields + line items) → `ready`.
- **prompt:** `extract_freeform(text, prompt, output_format)`; `json` output → `extracted.json` + `link_dict_to_bboxes()` (generic field links, no line items); `text` output → `result.txt` (linking skipped) → `ready`.

### Env vars consumed by the worker

- LLM provider config — via `extract.resolve_llm_config()`: `LLM_PROVIDER` (`ollama`/`openrouter`) + that profile's `*_BASE_URL`/`*_API_KEY`/`*_MODEL`/`*_STRUCTURED_MODE`/`*_DISABLE_THINKING`, or the legacy flat vars when `LLM_PROVIDER` is unset.
- `OCR_MIN_SCORE` — float 0.0..1.0, default `0.5`. OCR tokens with `score < OCR_MIN_SCORE` are dropped before being passed to the LLM. Raise to be stricter (drops more low-confidence noise), lower to keep faded/handwritten text.
- Upload/render caps (`app/main.py` + `pdf_ocr.py`): `MAX_UPLOAD_MB` (25), `MAX_PDF_PAGES` (100), `MAX_PAGE_MEGAPIXELS` (40).

`app/main.py` imports this:
```python
from app.jobs import run_job
...
background_tasks.add_task(run_job, job_id)
```

---

## Frontend types (`frontend/src/api/types.ts`)

TypeScript mirrors of the Pydantic models above. Authoritative definitions Agent C writes:

```ts
export type Polygon = [number, number][];   // length 4

export type JobStatus =
  | "created"
  | "queued" | "ocr" | "extract" | "linking"
  | "ready" | "approved" | "failed";

export interface Rect { x: number; y: number; w: number; h: number }
export interface PageROIs { page: number; rects: Rect[] }
export interface StartJobRequest {
  rois: PageROIs[];
  language: "en" | "fa";
  mode: "invoice" | "prompt";
  prompt?: string | null;                 // required when mode === "prompt"
  output_format?: "text" | "json" | null; // required when mode === "prompt"
}

export interface PageInfo {
  page: number;
  image_url: string;
  width: number;
  height: number;
}

export interface FieldLink {
  field_path: string;
  value: string | number | null;
  page: number | null;
  bbox: Polygon | null;
  score: number;
  edited: boolean;
}

export interface BankAccount {
  currency: string | null;        // ISO 4217 code
  account_number: string | null;
  iban: string | null;
  extra: string | null;
}

export interface BankInfo {
  bank_name: string | null;
  swift_code: string | null;
  bank_country: string | null;    // full country name decoded from SWIFT
  address: string | null;
  beneficiary: string | null;
  accounts: BankAccount[];        // one per currency
  notices: string[];              // wire fee / memo / intermediary notes
  account_number: string | null;  // legacy single-account fallback
}

export interface LineItem {
  no: number | null;
  product_name: string | null;
  category: string | null;        // high-level domain inferred from product_name
  count_type: "piece" | "weight" | null;
  unit: string | null;            // "pcs", "kg", "box", "carton", ...
  quantity: number | null;
  unit_price: number | null;
  line_currency: string | null;   // ISO 4217 if differs from invoice currency
  line_total: number | null;
  // legacy / extra detail
  size: string | null;
  colour: string | null;
  weight_per_box: number | null;
  box_per_carton: string | null;
  piece_per_box: string | null;
  quantity_carton: number | null;
  quantity_box: number | null;
  fob_unit_price_usd: number | null;
  aggregate_amount: number | null;
}

export interface LineItemLink {
  row_index: number;
  data: LineItem;
  page: number | null;
  bbox: Polygon | null;
  edited: boolean;
}

export interface Invoice {
  // primary fields
  company_name: string | null;
  company_address: string | null;
  company_country: string | null;     // FULL country name (e.g. "United Arab Emirates")
  company_phone: string | null;
  invoice_number: string | null;
  invoice_date: string | null;        // ISO YYYY-MM-DD when parseable
  currency: string | null;            // ISO 4217 (USD, AED, EUR, CNY, ...)
  total_amount: number | null;
  line_items: LineItem[];
  bank: BankInfo;
  extra_notes: string[];              // anything not captured by typed fields

  // legacy / optional
  invoice_type: string | null;
  customer_name: string | null;
  consignee_name: string | null;
  consignee_address: string | null;
  exporter_name: string | null;
  exporter_address: string | null;
  exporter_tel: string | null;
  payable_to: string | null;
  incoterm: string | null;
  total_quantity_carton: number | null;
  total_quantity_box: number | null;
  total_aggregate_amount: number | null;   // legacy alias of total_amount
  deposit_received: number | null;
  deposit_date: string | null;
  remain_money: number | null;
  payment_terms: string | null;
  packing: string | null;
  remarks: string[];
}

// Discriminated by `kind`; only the fields for that kind are populated.
export interface JobResult {
  kind: "invoice" | "prompt_json" | "prompt_text";
  invoice?: Invoice | null;          // kind === "invoice"
  data?: Record<string, unknown> | null; // kind === "prompt_json" (raw inferred dict)
  text?: string | null;              // kind === "prompt_text"
  fields: FieldLink[];               // invoice + prompt_json
  line_items: LineItemLink[];        // invoice only
  pages: PageInfo[];                 // all kinds
}

export interface JobDetail {
  id: string;
  source_filename: string;
  page_count: number | null;
  status: JobStatus;
  progress_pct: number;
  mode: "invoice" | "prompt";
  output_format: "text" | "json" | null;
  error: string | null;
  created_at: string;
  updated_at: string;
  result: JobResult | null;
}

export interface JobSummary {
  id: string;
  source_filename: string;
  page_count: number | null;
  status: JobStatus;
  progress_pct: number;
  created_at: string;
}

export interface OcrToken {
  text: string;
  bbox: Polygon;
  score: number;
}

export interface OcrPageTokens {
  page: number;
  width: number;
  height: number;
  tokens: OcrToken[];
}

export interface OcrPayload {
  pages: OcrPageTokens[];
}
```

---

## Selection state (`frontend/src/state/useReview.ts`)

Zustand store. Agent C writes the implementation; Agents D and E import the hook.

```ts
import { create } from "zustand";

interface ReviewState {
  jobId: string | null;
  detail: JobDetail | null;
  ocr: OcrPayload | null;

  activeFieldPath: string | null;   // selected field (highlight + scroll target)
  hoverFieldPath: string | null;    // softer highlight on hover
  activePage: number;               // 1-indexed; which page is visible in PdfPane

  setJobId(id: string): void;
  setDetail(d: JobDetail): void;
  setOcr(o: OcrPayload): void;
  setActiveField(path: string | null): void;
  setHoverField(path: string | null): void;
  setActivePage(page: number): void;

  /** PATCH /api/jobs/{id}/fields/{path}; updates local detail.result.fields */
  patchField(path: string, patch: Partial<{ value: unknown; page: number; bbox: Polygon }>): Promise<void>;
  addLineItem(data: LineItem): Promise<void>;
  patchLineItem(rowIndex: number, patch: Partial<{ data: LineItem; page: number; bbox: Polygon }>): Promise<void>;
  deleteLineItem(rowIndex: number): Promise<void>;
  approve(): Promise<void>;
}

export const useReview = create<ReviewState>(/* ... */);
```

Selection rule: when a `FieldLink` is selected and its `page` differs from `activePage`, also call `setActivePage(link.page)` so the PDF pane jumps to that page.

---

## Vite proxy (`frontend/vite.config.ts`)

```ts
server: {
  port: 5173,
  proxy: {
    "/api": "http://localhost:8000",
  },
}
```

So frontend calls `fetch("/api/jobs")` and Vite proxies to FastAPI in dev. In prod we'd serve them under the same origin.

---

## Field path naming convention

Used everywhere (DB `fields.field_path`, API URLs, frontend state keys). In invoice mode it follows `Invoice`'s leaves; in prompt/json mode the same convention applies to the inferred dict's leaves (emitted by `link_dict_to_bboxes`). Dot-notation, leaf-only:

- Top-level scalar: `invoice_number`, `company_name`, `currency`, `total_amount`
- Nested model scalar (BankInfo): `bank.bank_name`, `bank.swift_code`, `bank.bank_country`, `bank.address`, `bank.beneficiary`, `bank.account_number` (legacy)
- Top-level string list: `remarks[0]`, `extra_notes[0]`, `extra_notes[1]`, ...
- Nested string list: `bank.notices[0]`, `bank.notices[1]`, ...
- Nested model list (BankAccount): `bank.accounts[0].currency`, `bank.accounts[0].account_number`, `bank.accounts[0].iban`, ...
- Line items are **not** part of `fields[]`. They live in `line_items[]` with their own `row_index`.

The `walk_invoice_leaves()` helper in `app/linking.py` yields `(field_path, value)` tuples following this convention.

---

## Status FSM

```
created (pages rendered, awaiting Start)
   │
   │  POST /api/jobs/{id}/start  (rois + mode/prompt persisted, run_job enqueued)
   ▼
queued → ocr → extract → linking → ready ──► approved
   │       │       │         │
   └───────┴───────┴─────────┴───► failed
```

The `extract`/`linking` states are shared across modes. In **prompt+text** mode there is no field→bbox linking, so the worker goes `extract → ready` (the `linking` state is skipped); invoice and prompt+json both pass through `linking`. A `failed` job can be restarted via `POST .../start` (partial rows cleared first).

`progress_pct` rough heuristic:
- created: 0  (user is configuring the run / drawing ROIs)
- queued: 0
- ocr: 10..70 (scales with page count)
- extract: ~75
- linking: ~88 (invoice + prompt/json only)
- ready: 100
- approved: 100
