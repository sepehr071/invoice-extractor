import { useEffect, useState } from "react";
import styles from "./FieldsPane.module.css";

const SECTION_STORAGE_PREFIX = "invoice-ai.sections.";

/** Persisted expand/collapse state for a section, keyed by its id. */
function loadExpanded(sectionId: string, fallback: boolean): boolean {
  if (typeof window === "undefined") return fallback;
  const raw = window.localStorage.getItem(SECTION_STORAGE_PREFIX + sectionId);
  if (raw === null) return fallback;
  return raw === "1";
}

export type LinkSummary = { linked: number; total: number };

export interface SectionProps {
  id: string;
  title: string;
  count?: number | null;
  summary?: LinkSummary | null;
  defaultExpanded?: boolean;
  children: React.ReactNode;
  sectionRef?: (el: HTMLElement | null) => void;
}

/**
 * Collapsible section card — shared by the invoice FieldsPane and the
 * prompt_json GenericFieldsPane. Persists its expanded state to localStorage
 * and responds to a "invoice-ai:expand-section" CustomEvent (dispatched by
 * the chip bar) so an external jump can force-open a collapsed section.
 */
export function Section({
  id,
  title,
  count,
  summary,
  defaultExpanded = true,
  children,
  sectionRef,
}: SectionProps) {
  const [expanded, setExpanded] = useState<boolean>(() => loadExpanded(id, defaultExpanded));

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(SECTION_STORAGE_PREFIX + id, expanded ? "1" : "0");
  }, [id, expanded]);

  useEffect(() => {
    const onExpand = (e: Event) => {
      const detail = (e as CustomEvent<{ id: string }>).detail;
      if (detail?.id === id) setExpanded(true);
    };
    window.addEventListener("invoice-ai:expand-section", onExpand as EventListener);
    return () =>
      window.removeEventListener("invoice-ai:expand-section", onExpand as EventListener);
  }, [id]);

  return (
    <section
      id={`section-${id}`}
      data-section-id={id}
      ref={(el) => sectionRef?.(el)}
      className={`${styles.section} ${expanded ? "" : styles.sectionCollapsed}`}
    >
      <button
        type="button"
        className={styles.sectionHeader}
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        aria-controls={`section-body-${id}`}
      >
        <span className={styles.sectionCaret} aria-hidden="true">
          {expanded ? "▾" : "▸"}
        </span>
        <span className={styles.sectionTitleText}>{title}</span>
        {typeof count === "number" && (
          <span className={styles.sectionCount}>{count}</span>
        )}
        {summary && summary.total > 0 && (
          <span
            className={`${styles.sectionLinked} ${
              summary.linked === summary.total ? styles.sectionLinkedFull : ""
            }`}
            title={`${summary.linked} of ${summary.total} fields linked to a bbox`}
          >
            {summary.linked}/{summary.total} linked
          </span>
        )}
      </button>
      {expanded && (
        <div id={`section-body-${id}`} className={styles.sectionBody}>
          {children}
        </div>
      )}
    </section>
  );
}

export default Section;
