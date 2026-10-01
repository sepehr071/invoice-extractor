import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { useReview } from "../state/useReview";
import type { FieldLink } from "../api/types";
import { recordLastSave } from "./lastSave";
import styles from "./FieldRow.module.css";

interface Props {
  fieldPath: string;
  label: string;
  value: string | number | null;
  link: FieldLink | undefined;
  type?: "text" | "number" | "date";
  disabled?: boolean;
  /** When true, render the rapidfuzz score number alongside the status glyph. */
  showConfidence?: boolean;
}

type Tier = "high" | "mid" | "low" | "none";

function scoreTier(link: FieldLink | undefined): Tier {
  // SCORE_THRESHOLD = 60 in app/linking.py — below this the field is "unlinked".
  if (!link || link.bbox === null) return "none";
  const s = link.score;
  if (!Number.isFinite(s) || s <= 0) return "none";
  if (s >= 85) return "high";
  if (s >= 60) return "mid";
  return "low";
}

function formatValue(value: string | number | null, type: "text" | "number" | "date"): string {
  if (value === null || value === undefined) return "";
  if (type === "number" && typeof value === "number") return String(value);
  return String(value);
}

function parseValue(raw: string, type: "text" | "number" | "date"): string | number | null {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  if (type === "number") {
    const n = Number(trimmed);
    return Number.isFinite(n) ? n : null;
  }
  return trimmed;
}

// Distinct SHAPE per tier so confidence is legible without relying on color
// (this is a compliance tool — must pass color-blind reviewers).
function glyphFor(tier: Tier): string {
  if (tier === "high") return "●"; // filled — strong match
  if (tier === "mid") return "◑";  // half — medium confidence
  if (tier === "low") return "○";  // hollow — weak match
  return "—";                       // none — unlinked
}

function tooltipFor(tier: Tier, link: FieldLink | undefined): string {
  if (tier === "none") return "Unlinked — click PDF to set bbox";
  const score = link?.score ?? 0;
  if (tier === "high") return `Linked · score ${score.toFixed(1)}`;
  if (tier === "mid") return `Low confidence · score ${score.toFixed(1)}`;
  return `Linked (weak) · score ${score.toFixed(1)}`;
}

export function FieldRow({
  fieldPath,
  label,
  value,
  link,
  type = "text",
  disabled = false,
  showConfidence = false,
}: Props) {
  const activeFieldPath = useReview((s) => s.activeFieldPath);
  const setActiveField = useReview((s) => s.setActiveField);
  const setHoverField = useReview((s) => s.setHoverField);

  const [draft, setDraft] = useState<string>(formatValue(value, type));
  const focusedRef = useRef(false);

  useEffect(() => {
    if (!focusedRef.current) {
      setDraft(formatValue(value, type));
    }
  }, [value, type]);

  const active = activeFieldPath === fieldPath;

  const commit = () => {
    const parsed = parseValue(draft, type);
    const current = value ?? null;
    const same =
      (parsed === null && current === null) ||
      (parsed !== null && current !== null && String(parsed) === String(current));
    if (same) return;
    const run = () => void useReview.getState().patchField(fieldPath, { value: parsed });
    recordLastSave(run);
    run();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.currentTarget.blur();
    } else if (e.key === "Escape") {
      setDraft(formatValue(value, type));
      e.currentTarget.blur();
    }
  };

  const tier = scoreTier(link);
  const edited = link?.edited === true;
  const tierClass =
    tier === "high"
      ? styles.tierHigh
      : tier === "mid"
      ? styles.tierMid
      : tier === "low"
      ? styles.tierLow
      : styles.tierNone;
  const rowTintClass = edited ? styles.tierEdited : tierClass;
  const linked = tier !== "none";
  const scoreNum = link && link.score > 0 ? link.score.toFixed(0) : null;
  const a11yLabel =
    tier === "none"
      ? "Unlinked"
      : tier === "high"
      ? `High confidence, score ${scoreNum}`
      : tier === "mid"
      ? `Medium confidence, score ${scoreNum}`
      : `Low confidence, score ${scoreNum}`;

  return (
    <div
      className={`${styles.row} ${rowTintClass} ${active ? styles.active : ""}`}
      onMouseEnter={() => setHoverField(fieldPath)}
      onMouseLeave={() => setHoverField(null)}
      onClick={() => setActiveField(fieldPath)}
    >
      <label className={styles.label} htmlFor={`f-${fieldPath}`}>
        {edited && (
          <span className={styles.editedDot} title="Edited by reviewer" aria-label="edited" />
        )}
        <span className={styles.labelText}>{label}</span>
      </label>
      <div className={styles.inputWrap}>
        <input
          id={`f-${fieldPath}`}
          className={styles.input}
          type={type === "number" ? "number" : "text"}
          dir="auto"
          value={draft}
          disabled={disabled}
          onChange={(e) => setDraft(e.target.value)}
          onFocus={() => {
            focusedRef.current = true;
            setActiveField(fieldPath);
          }}
          onBlur={() => {
            focusedRef.current = false;
            commit();
          }}
          onKeyDown={onKeyDown}
          onClick={(e) => e.stopPropagation()}
          placeholder="—"
          step={type === "number" ? "any" : undefined}
        />
        <div className={styles.status} title={tooltipFor(tier, link)} aria-label={a11yLabel}>
          {/* Inline numeric score for linked fields — never buried in a tooltip.
              `showConfidence` makes it more prominent; otherwise it stays muted. */}
          {linked && scoreNum && (
            <span className={`${styles.scoreNum} ${showConfidence ? styles.scoreNumStrong : ""}`}>
              {scoreNum}
            </span>
          )}
          <span className={`${styles.glyph} ${tierClass}`} aria-hidden="true">
            {glyphFor(tier)}
          </span>
        </div>
      </div>
    </div>
  );
}

export default FieldRow;
