import { create } from "zustand";
import {
  addLineItem as apiAddLineItem,
  approve as apiApprove,
  deleteLineItem as apiDeleteLineItem,
  getJob,
  patchField as apiPatchField,
  patchLineItem as apiPatchLineItem,
} from "../api/client";
import type {
  FieldLink,
  JobDetail,
  JobResult,
  LineItem,
  LineItemLink,
  OcrPayload,
  Polygon,
} from "../api/types";

export type SaveStatus = "idle" | "saving" | "saved" | "error";

/** Editable field links exist on invoice + prompt_json results, never prompt_text. */
function resultFields(result: JobResult | null | undefined): FieldLink[] {
  if (result && (result.kind === "invoice" || result.kind === "prompt_json")) {
    return result.fields;
  }
  return [];
}

/** Line-item links exist only on the invoice variant. */
function resultLineItems(result: JobResult | null | undefined): LineItemLink[] {
  if (result && result.kind === "invoice") return result.line_items;
  return [];
}

interface ReviewState {
  jobId: string | null;
  detail: JobDetail | null;
  ocr: OcrPayload | null;

  activeFieldPath: string | null;
  hoverFieldPath: string | null;
  activePage: number;
  saveStatus: SaveStatus;
  saveError: string | null;

  setJobId(id: string): void;
  setDetail(d: JobDetail): void;
  setOcr(o: OcrPayload): void;
  setActiveField(path: string | null): void;
  setHoverField(path: string | null): void;
  setActivePage(page: number): void;

  patchField(
    path: string,
    patch: Partial<{ value: unknown; page: number; bbox: Polygon }>
  ): Promise<void>;
  addLineItem(data: LineItem): Promise<void>;
  patchLineItem(
    rowIndex: number,
    patch: Partial<{ data: LineItem; page: number; bbox: Polygon }>
  ): Promise<void>;
  deleteLineItem(rowIndex: number): Promise<void>;
  approve(): Promise<void>;
}

let savedTimer: number | undefined;
function flashSaved(set: (p: Partial<ReviewState>) => void) {
  if (savedTimer) window.clearTimeout(savedTimer);
  set({ saveStatus: "saved", saveError: null });
  savedTimer = window.setTimeout(() => set({ saveStatus: "idle" }), 1400);
}

function replaceField(fields: FieldLink[], updated: FieldLink): FieldLink[] {
  const idx = fields.findIndex((f) => f.field_path === updated.field_path);
  if (idx === -1) return [...fields, updated];
  const next = fields.slice();
  next[idx] = updated;
  return next;
}

function replaceLineItem(
  items: LineItemLink[],
  updated: LineItemLink
): LineItemLink[] {
  const idx = items.findIndex((li) => li.row_index === updated.row_index);
  if (idx === -1) return [...items, updated];
  const next = items.slice();
  next[idx] = updated;
  return next;
}

export const useReview = create<ReviewState>((set, get) => ({
  jobId: null,
  detail: null,
  ocr: null,
  activeFieldPath: null,
  hoverFieldPath: null,
  activePage: 1,
  saveStatus: "idle",
  saveError: null,

  setJobId: (id) => set({ jobId: id }),

  setDetail: (d) => {
    const cur = get();
    // Reset activePage to 1 when loading a different job
    const activePage =
      cur.detail?.id === d.id ? cur.activePage : 1;
    set({ detail: d, activePage });
  },

  setOcr: (o) => set({ ocr: o }),

  setActiveField: (path) => {
    const { detail, activePage } = get();
    if (path && detail?.result) {
      // Line-item rows use a `line_item:<idx>` path — look those up in the
      // invoice variant's line_items, not its fields (fixes page-jump for rows).
      if (path.startsWith("line_item:")) {
        const idx = Number(path.slice("line_item:".length));
        const li = resultLineItems(detail.result).find((l) => l.row_index === idx);
        if (li && li.page != null && li.page !== activePage) {
          set({ activeFieldPath: path, activePage: li.page });
          return;
        }
      } else {
        const link = resultFields(detail.result).find((f) => f.field_path === path);
        if (link && link.page != null && link.page !== activePage) {
          set({ activeFieldPath: path, activePage: link.page });
          return;
        }
      }
    }
    set({ activeFieldPath: path });
  },

  setHoverField: (path) => set({ hoverFieldPath: path }),
  setActivePage: (page) => set({ activePage: page }),

  patchField: async (path, patch) => {
    const { jobId, detail } = get();
    if (!jobId || !detail?.result) return;
    const result = detail.result;
    // Field PATCH only applies to variants that carry `fields`.
    if (result.kind !== "invoice" && result.kind !== "prompt_json") return;
    set({ saveStatus: "saving" });
    try {
      const updated = await apiPatchField(jobId, path, patch);
      set({
        detail: {
          ...detail,
          result: {
            ...result,
            fields: replaceField(result.fields, updated),
          },
        },
      });
      flashSaved(set);
    } catch (e) {
      set({ saveStatus: "error", saveError: (e as Error).message });
    }
  },

  addLineItem: async (data) => {
    const { jobId, detail } = get();
    if (!jobId || !detail?.result || detail.result.kind !== "invoice") return;
    const result = detail.result;
    set({ saveStatus: "saving" });
    try {
      const created = await apiAddLineItem(jobId, { data });
      set({
        detail: {
          ...detail,
          result: {
            ...result,
            line_items: [...result.line_items, created],
          },
        },
      });
      flashSaved(set);
    } catch (e) {
      set({ saveStatus: "error", saveError: (e as Error).message });
    }
  },

  patchLineItem: async (rowIndex, patch) => {
    const { jobId, detail } = get();
    if (!jobId || !detail?.result || detail.result.kind !== "invoice") return;
    const result = detail.result;
    set({ saveStatus: "saving" });
    try {
      const updated = await apiPatchLineItem(jobId, rowIndex, patch);
      set({
        detail: {
          ...detail,
          result: {
            ...result,
            line_items: replaceLineItem(result.line_items, updated),
          },
        },
      });
      flashSaved(set);
    } catch (e) {
      set({ saveStatus: "error", saveError: (e as Error).message });
    }
  },

  deleteLineItem: async (rowIndex) => {
    const { jobId, detail } = get();
    if (!jobId || !detail?.result || detail.result.kind !== "invoice") return;
    const result = detail.result;
    set({ saveStatus: "saving" });
    try {
      await apiDeleteLineItem(jobId, rowIndex);
      set({
        detail: {
          ...detail,
          result: {
            ...result,
            line_items: result.line_items.filter((li) => li.row_index !== rowIndex),
          },
        },
      });
      flashSaved(set);
    } catch (e) {
      set({ saveStatus: "error", saveError: (e as Error).message });
    }
  },

  approve: async () => {
    const { jobId } = get();
    if (!jobId) return;
    set({ saveStatus: "saving" });
    try {
      await apiApprove(jobId);
      const fresh = await getJob(jobId);
      set({ detail: fresh });
      flashSaved(set);
    } catch (e) {
      set({ saveStatus: "error", saveError: (e as Error).message });
      throw e;
    }
  },
}));
