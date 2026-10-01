export type Polygon = [number, number][]; // length 4

export type JobStatus =
  | "created"
  | "queued"
  | "ocr"
  | "extract"
  | "linking"
  | "ready"
  | "approved"
  | "failed";

/** Job mode chosen on the run-config screen. `prompt` is the default. */
export type Mode = "prompt" | "invoice";

/** Output shape for a `prompt` job — free-form markdown text or inferred JSON. */
export type OutputFormat = "text" | "json";

/**
 * Recursive JSON value — the shape of `prompt_json` results, which carry
 * arbitrary nested keys inferred by the backend from the user's prompt.
 */
export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface PageROIs {
  page: number;    // 1-indexed
  rects: Rect[];
}

/** Per-job OCR language. Backend maps fa→Persian/arabic recognizer, en→English. */
export type Language = "en" | "fa";

export interface StartJobRequest {
  rois: PageROIs[];
  language: Language;
  mode: Mode;
  /** Required when mode === "prompt". */
  prompt?: string;
  /** Required when mode === "prompt". */
  output_format?: OutputFormat;
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

export type CountType = "piece" | "weight";

export interface LineItem {
  no: number | null;
  product_name: string | null;
  category: string | null;        // high-level domain inferred from product_name
  count_type: CountType | null;
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
  extra_notes: string[];

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

/**
 * Discriminated union on `kind`, returned under `JobDetail.result`.
 *
 * - `invoice`     — the structured invoice review flow (fields + line items).
 * - `prompt_json` — arbitrary inferred JSON; editable links live under `fields`
 *                   (same key as invoice so PdfPane / store reuse is unchanged).
 *                   No `line_items`.
 * - `prompt_text` — a markdown string with no field links or overlays.
 */
export interface InvoiceResult {
  kind: "invoice";
  invoice: Invoice;
  fields: FieldLink[];
  line_items: LineItemLink[];
  pages: PageInfo[];
}

export interface PromptJsonResult {
  kind: "prompt_json";
  data: Record<string, JsonValue>;
  fields: FieldLink[];
  pages: PageInfo[];
}

export interface PromptTextResult {
  kind: "prompt_text";
  text: string;
  pages: PageInfo[];
}

export type JobResult = InvoiceResult | PromptJsonResult | PromptTextResult;

export interface JobDetail {
  id: string;
  source_filename: string;
  page_count: number | null;
  status: JobStatus;
  progress_pct: number;
  mode: Mode;
  output_format: OutputFormat | null;
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
