import { useEffect, useMemo, useState } from "react";
import { useReview } from "../state/useReview";
import type { FieldLink, JsonValue } from "../api/types";
import FieldRow from "./FieldRow";
import { Section } from "./Section";
import styles from "./FieldsPane.module.css";

const CONFIDENCE_STORAGE_KEY = "invoice-ai.showConfidence";

/** A single editable scalar leaf flattened out of the inferred JSON tree. */
interface Leaf {
  path: string;
  label: string;
  value: string | number | null;
  type: "text" | "number";
}

/** Last dot/bracket segment of a path → a human label ("a.b[2].c" → "c [2]"). */
function leafLabel(path: string): string {
  const lastDot = path.lastIndexOf(".");
  const tail = lastDot >= 0 ? path.slice(lastDot + 1) : path;
  // Surface a trailing array index on the parent key, e.g. "items[2]" → "items 3".
  const m = tail.match(/^(.*)\[(\d+)\]$/);
  if (m) {
    const base = m[1] || (lastDot >= 0 ? path.slice(0, lastDot).split(".").pop() ?? "" : "");
    return `${base || "item"} ${Number(m[2]) + 1}`;
  }
  return tail;
}

/**
 * Flatten an arbitrary JSON value into editable scalar leaves, mirroring the
 * backend dot-path convention exactly:
 *   - object key       → `${prefix}.${key}` (or `key` at the root)
 *   - array element    → `${prefix}[i]`
 *   - scalar           → emitted as a Leaf
 * Booleans render as the strings "true"/"false". null is an editable empty leaf.
 */
function flatten(value: JsonValue, prefix: string, out: Leaf[]): void {
  if (value === null) {
    out.push({ path: prefix, label: leafLabel(prefix), value: null, type: "text" });
    return;
  }
  if (typeof value === "string") {
    out.push({ path: prefix, label: leafLabel(prefix), value, type: "text" });
    return;
  }
  if (typeof value === "number") {
    out.push({ path: prefix, label: leafLabel(prefix), value, type: "number" });
    return;
  }
  if (typeof value === "boolean") {
    out.push({ path: prefix, label: leafLabel(prefix), value: String(value), type: "text" });
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((item, i) => flatten(item, `${prefix}[${i}]`, out));
    return;
  }
  // object
  for (const [k, v] of Object.entries(value)) {
    flatten(v, prefix ? `${prefix}.${k}` : k, out);
  }
}

/** A renderable top-level group: one section per root key of `data`. */
interface Group {
  id: string;
  title: string;
  leaves: Leaf[];
}

function buildGroups(data: Record<string, JsonValue>): Group[] {
  return Object.entries(data).map(([key, value]) => {
    const leaves: Leaf[] = [];
    flatten(value, key, leaves);
    return { id: key, title: key, leaves };
  });
}

/**
 * GenericFieldsPane — renders a `prompt_json` result. The inferred JSON object
 * is flattened to dot-paths; each scalar leaf is an editable FieldRow wired to
 * the same field PATCH flow + bbox highlight as the invoice pane (links live
 * under `result.fields`, keyed by field_path).
 */
export function GenericFieldsPane() {
  const detail = useReview((s) => s.detail);
  const result = detail?.result;
  const promptResult = result && result.kind === "prompt_json" ? result : null;
  const disabled = detail?.status === "approved";

  const [showConfidence, setShowConfidence] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return window.localStorage.getItem(CONFIDENCE_STORAGE_KEY) === "1";
  });

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(CONFIDENCE_STORAGE_KEY, showConfidence ? "1" : "0");
  }, [showConfidence]);

  const fieldsByPath = useMemo(() => {
    const map: Record<string, FieldLink> = {};
    for (const f of promptResult?.fields ?? []) map[f.field_path] = f;
    return map;
  }, [promptResult]);

  const groups = useMemo(
    () => (promptResult ? buildGroups(promptResult.data) : []),
    [promptResult]
  );

  if (!promptResult) {
    return (
      <div className={styles.pane}>
        <div className={styles.placeholder}>Waiting for extraction results…</div>
      </div>
    );
  }

  const totalLeaves = groups.reduce((acc, g) => acc + g.leaves.length, 0);

  return (
    <div className={`${styles.pane} ${disabled ? styles.disabled : ""}`}>
      <div className={styles.chipBar}>
        <div className={styles.chipRow}>
          <span className={styles.sectionTitleText}>Extracted JSON</span>
        </div>
        <button
          type="button"
          className={`${styles.toggleBtn} ${showConfidence ? styles.toggleBtnOn : ""}`}
          onClick={() => setShowConfidence((v) => !v)}
          title="Show or hide the match score (0..100) on every linked field"
          aria-pressed={showConfidence}
        >
          <span className={styles.toggleDot} aria-hidden="true" />
          {showConfidence ? "Confidence: ON" : "Show confidence"}
        </button>
      </div>

      {disabled && (
        <div className={styles.approvedBanner}>
          This result has been finalized. Fields are read-only.
        </div>
      )}

      {totalLeaves === 0 ? (
        <div className={styles.emptyExtraction} role="status">
          <span className={styles.emptyExtractionIcon} aria-hidden="true">!</span>
          <div>
            <strong>No structured data returned.</strong>
            <p>
              The model produced an empty JSON object for this prompt. Review the document on
              the left, or refine the prompt and run a new job.
            </p>
          </div>
        </div>
      ) : (
        groups.map((group) => (
          <Section key={group.id} id={`json-${group.id}`} title={group.title} defaultExpanded>
            {group.leaves.length === 0 ? (
              <div className={styles.remarkEmpty}>Empty.</div>
            ) : (
              group.leaves.map((leaf) => (
                <FieldRow
                  key={leaf.path}
                  fieldPath={leaf.path}
                  label={leaf.label}
                  type={leaf.type}
                  value={leaf.value}
                  link={fieldsByPath[leaf.path]}
                  disabled={disabled}
                  showConfidence={showConfidence}
                />
              ))
            )}
          </Section>
        ))
      )}
    </div>
  );
}

export default GenericFieldsPane;
