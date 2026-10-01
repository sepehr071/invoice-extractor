import type {
  FieldLink,
  JobDetail,
  JobSummary,
  Language,
  LineItem,
  LineItemLink,
  Mode,
  OcrPayload,
  OutputFormat,
  PageROIs,
  Polygon,
  StartJobRequest,
} from "./types";

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
      else if (typeof body === "string") detail = body;
    } catch {
      // ignore non-JSON bodies
    }
    throw new Error(`${res.status} ${detail}`);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

function jsonInit(method: string, body?: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  };
}

/** Upload a PDF or image. Backend renders pages and creates a job (status=created). */
export async function uploadPdf(
  file: File
): Promise<{ job_id: string; page_count: number }> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch("/api/jobs", { method: "POST", body: form });
  return handle(res);
}

export interface StartJobOptions {
  rois: PageROIs[];
  language?: Language;
  mode: Mode;
  /** Required when mode === "prompt". */
  prompt?: string;
  /** Required when mode === "prompt". */
  output_format?: OutputFormat;
}

export async function startJob(
  id: string,
  opts: StartJobOptions
): Promise<{ status: string }> {
  const body: StartJobRequest = {
    rois: opts.rois,
    language: opts.language ?? "en",
    mode: opts.mode,
    ...(opts.prompt !== undefined ? { prompt: opts.prompt } : {}),
    ...(opts.output_format !== undefined ? { output_format: opts.output_format } : {}),
  };
  const res = await fetch(
    `/api/jobs/${encodeURIComponent(id)}/start`,
    jsonInit("POST", body)
  );
  return handle(res);
}

export async function getRois(id: string): Promise<{ rois: PageROIs[] }> {
  const res = await fetch(`/api/jobs/${encodeURIComponent(id)}/rois`);
  return handle(res);
}

export async function listJobs(): Promise<JobSummary[]> {
  const res = await fetch("/api/jobs");
  return handle(res);
}

export async function getJob(id: string): Promise<JobDetail> {
  const res = await fetch(`/api/jobs/${encodeURIComponent(id)}`);
  return handle(res);
}

export async function getOcr(id: string): Promise<OcrPayload> {
  const res = await fetch(`/api/jobs/${encodeURIComponent(id)}/ocr`);
  return handle(res);
}

export async function patchField(
  id: string,
  fieldPath: string,
  patch: { value?: unknown; page?: number; bbox?: Polygon }
): Promise<FieldLink> {
  const res = await fetch(
    `/api/jobs/${encodeURIComponent(id)}/fields/${encodeURIComponent(fieldPath)}`,
    jsonInit("PATCH", patch)
  );
  return handle(res);
}

/** Persist the edited markdown text result (prompt_text mode). */
export async function patchText(id: string, text: string): Promise<{ text: string }> {
  const res = await fetch(
    `/api/jobs/${encodeURIComponent(id)}/text`,
    jsonInit("PATCH", { text })
  );
  return handle(res);
}

export async function addLineItem(
  id: string,
  body: { data: LineItem; page?: number; bbox?: Polygon }
): Promise<LineItemLink> {
  const res = await fetch(
    `/api/jobs/${encodeURIComponent(id)}/line-items`,
    jsonInit("POST", body)
  );
  return handle(res);
}

export async function patchLineItem(
  id: string,
  rowIndex: number,
  patch: { data?: LineItem; page?: number; bbox?: Polygon }
): Promise<LineItemLink> {
  const res = await fetch(
    `/api/jobs/${encodeURIComponent(id)}/line-items/${rowIndex}`,
    jsonInit("PATCH", patch)
  );
  return handle(res);
}

export async function deleteLineItem(
  id: string,
  rowIndex: number
): Promise<void> {
  const res = await fetch(
    `/api/jobs/${encodeURIComponent(id)}/line-items/${rowIndex}`,
    { method: "DELETE" }
  );
  await handle<void>(res);
}

/** Approve a job. Mode-aware on the backend; returns the approved payload. */
export async function approve(id: string): Promise<unknown> {
  const res = await fetch(
    `/api/jobs/${encodeURIComponent(id)}/approve`,
    jsonInit("POST", {})
  );
  return handle(res);
}

export function pageImageUrl(id: string, page: number, variant?: string): string {
  // Optional `variant` busts the browser cache when the same URL needs to
  // surface different content (e.g. original page during Preview vs. masked
  // page during Review).
  const base = `/api/jobs/${encodeURIComponent(id)}/page/${page}.png`;
  return variant ? `${base}?v=${encodeURIComponent(variant)}` : base;
}
