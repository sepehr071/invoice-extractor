import { useEffect, useRef, useState } from "react";
import styles from "./PageNav.module.css";

interface Props {
  currentPage: number;
  pageCount: number;
  onChange: (page: number) => void;
}

/**
 * PageNav — small toolbar under the PDF image. Prev/Next buttons + numeric
 * jump-to-page input. ArrowLeft / ArrowRight on the window page through the
 * document when no input is focused.
 */
export function PageNav({ currentPage, pageCount, onChange }: Props) {
  const [draft, setDraft] = useState<string>(String(currentPage));
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Sync local draft when external page changes (e.g. via field selection).
  useEffect(() => {
    setDraft(String(currentPage));
  }, [currentPage]);

  // Global arrow-key navigation when the input isn't focused.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement | null)?.tagName?.toLowerCase();
      const isEditable =
        tag === "input" ||
        tag === "textarea" ||
        tag === "select" ||
        (e.target as HTMLElement | null)?.isContentEditable;
      if (isEditable) return;
      if (e.key === "ArrowLeft") {
        e.preventDefault();
        if (currentPage > 1) onChange(currentPage - 1);
      } else if (e.key === "ArrowRight") {
        e.preventDefault();
        if (currentPage < pageCount) onChange(currentPage + 1);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [currentPage, pageCount, onChange]);

  const commitDraft = () => {
    const n = Number.parseInt(draft, 10);
    if (Number.isFinite(n)) {
      const bounded = Math.max(1, Math.min(pageCount, n));
      onChange(bounded);
      setDraft(String(bounded));
    } else {
      setDraft(String(currentPage));
    }
  };

  return (
    <div className={styles.bar}>
      <button
        type="button"
        className={styles.btn}
        onClick={() => onChange(Math.max(1, currentPage - 1))}
        disabled={currentPage <= 1}
        aria-label="Previous page"
      >
        ‹ Prev
      </button>
      <div className={styles.pageInfo}>
        <span className={styles.label}>Page</span>
        <input
          ref={inputRef}
          type="number"
          min={1}
          max={pageCount}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commitDraft}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              commitDraft();
              inputRef.current?.blur();
            } else if (e.key === "Escape") {
              setDraft(String(currentPage));
              inputRef.current?.blur();
            }
          }}
          className={styles.input}
          aria-label="Jump to page"
        />
        <span className={styles.total}>/ {pageCount}</span>
      </div>
      <button
        type="button"
        className={styles.btn}
        onClick={() => onChange(Math.min(pageCount, currentPage + 1))}
        disabled={currentPage >= pageCount}
        aria-label="Next page"
      >
        Next ›
      </button>
    </div>
  );
}

export default PageNav;
