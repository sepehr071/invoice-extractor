import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useReview } from "../state/useReview";
import { getOcr } from "../api/client";
import { PdfPane } from "../components/PdfPane";
import { FieldsPane } from "../components/FieldsPane";
import { GenericFieldsPane } from "../components/GenericFieldsPane";
import { PromptTextView } from "../components/PromptTextView";
import { retryLastSave } from "../components/lastSave";
import ApproveButton from "../components/ApproveButton";
import styles from "./ReviewPage.module.css";

const STATUS_LABEL: Record<string, string> = {
  ready: "Ready",
  approved: "Approved",
  failed: "Failed",
  queued: "Working",
  ocr: "Reading",
  extract: "Reading",
  linking: "Reading",
};

export function ReviewPage() {
  const jobId = useReview((s) => s.jobId);
  const detail = useReview((s) => s.detail);
  const ocr = useReview((s) => s.ocr);
  const setOcr = useReview((s) => s.setOcr);
  const saveStatus = useReview((s) => s.saveStatus);
  const saveError = useReview((s) => s.saveError);

  const [ocrError, setOcrError] = useState(false);
  const [ocrLoading, setOcrLoading] = useState(false);
  const [showSource, setShowSource] = useState(false);

  const result = detail?.result ?? null;
  const kind = result?.kind ?? null;
  // OCR token map only matters where click-to-link overlays are shown.
  const needsOcr = kind === "invoice" || kind === "prompt_json";

  const loadOcr = useCallback(() => {
    if (!jobId) return;
    setOcrError(false);
    setOcrLoading(true);
    getOcr(jobId)
      .then((p) => {
        setOcr(p);
        setOcrLoading(false);
      })
      .catch((err) => {
        console.warn("OCR payload failed", err);
        setOcrError(true);
        setOcrLoading(false);
      });
  }, [jobId, setOcr]);

  useEffect(() => {
    if (!jobId || !needsOcr || ocr !== null) return;
    loadOcr();
  }, [jobId, needsOcr, ocr, loadOcr]);

  const fields = result && (result.kind === "invoice" || result.kind === "prompt_json")
    ? result.fields
    : [];
  const lineItems = result && result.kind === "invoice" ? result.line_items : [];

  const stats = useMemo(() => {
    const all = fields.length + lineItems.length;
    const linked =
      fields.filter((f) => f.bbox != null).length +
      lineItems.filter((li) => li.bbox != null).length;
    const edited =
      fields.filter((f) => f.edited).length +
      lineItems.filter((li) => li.edited).length;
    return { all, linked, edited };
  }, [fields, lineItems]);

  if (!detail) {
    return (
      <div className={styles.empty}>
        <p>No job loaded.</p>
      </div>
    );
  }

  const status = detail.status;
  const statusLabel = STATUS_LABEL[status] ?? status;
  const linkPct = stats.all === 0 ? 0 : Math.round((stats.linked / stats.all) * 100);
  const isText = kind === "prompt_text";
  const showLinkStats = kind === "invoice" || kind === "prompt_json";
  const showOverlays = kind === "invoice" || kind === "prompt_json";

  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <div className={styles.headerLeft}>
          <Link to="/" className={styles.backLink} title="Back to jobs">
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
              <path
                d="M9 2L4 7L9 12"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            <span>Jobs</span>
          </Link>
          <span className={styles.divider} aria-hidden="true" />
          <h1 className={styles.title} title={detail.source_filename}>
            {detail.source_filename}
          </h1>
          <span
            className={`${styles.badge} ${styles[`badge_${status}`] ?? ""}`}
            data-status={status}
          >
            {statusLabel}
          </span>
        </div>

        <div className={styles.headerRight}>
          {showLinkStats && (
            <div className={styles.stats} title="Linked / total fields">
              <span className={styles.statsValue}>
                {stats.linked}
                <span className={styles.statsTotal}>/{stats.all}</span>
              </span>
              <div className={styles.statsBar}>
                <div className={styles.statsFill} style={{ width: `${linkPct}%` }} />
              </div>
              <span className={styles.statsLabel}>linked</span>
            </div>
          )}

          {showLinkStats && stats.edited > 0 && (
            <div className={styles.editedChip}>
              <span className={styles.editedDot} />
              {stats.edited} edited
            </div>
          )}

          <SaveIndicator status={saveStatus} error={saveError} />

          <ApproveButton />
        </div>
      </header>

      {isText && result?.kind === "prompt_text" ? (
        <div className={styles.readingLayout}>
          <div className={styles.readingMain}>
            <PromptTextView text={result.text} />
          </div>

          <div className={styles.sourceSection}>
            <button
              type="button"
              className={styles.sourceToggle}
              onClick={() => setShowSource((v) => !v)}
              aria-expanded={showSource}
              aria-controls="original-document"
            >
              <span
                className={`${styles.sourceChev} ${showSource ? styles.sourceChevOpen : ""}`}
                aria-hidden="true"
              >
                ›
              </span>
              {showSource ? "Hide original document" : "Show original document"}
            </button>
            {showSource && (
              <div id="original-document" className={styles.sourcePane}>
                <PdfPane overlays={false} />
              </div>
            )}
          </div>
        </div>
      ) : (
        <div className={styles.split}>
          <aside className={styles.left}>
            <PdfPane overlays={showOverlays} />
          </aside>
          <section className={styles.right}>
            {ocrError && needsOcr && (
              <div className={styles.ocrNotice} role="status">
                <span className={styles.ocrNoticeIcon} aria-hidden="true">!</span>
                <span className={styles.ocrNoticeText}>
                  Source highlighting is unavailable for this document.
                </span>
                <button
                  type="button"
                  className={styles.ocrNoticeRetry}
                  onClick={loadOcr}
                  disabled={ocrLoading}
                >
                  {ocrLoading ? "Retrying…" : "Retry"}
                </button>
              </div>
            )}

            {result?.kind === "invoice" && <FieldsPane />}
            {result?.kind === "prompt_json" && <GenericFieldsPane />}
            {!result && (
              <div className={styles.empty}>
                <p>Waiting for the result…</p>
              </div>
            )}
          </section>
        </div>
      )}
    </div>
  );
}

function SaveIndicator({
  status,
  error,
}: {
  status: "idle" | "saving" | "saved" | "error";
  error: string | null;
}) {
  if (status === "idle") return null;
  if (status === "saving") {
    return (
      <span className={`${styles.save} ${styles.saveSaving}`}>
        <span className={styles.saveDot} />
        Saving
      </span>
    );
  }
  if (status === "saved") {
    return (
      <span className={`${styles.save} ${styles.saveSaved}`}>
        <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
          <path
            d="M2.5 6.5L5 9L9.5 3.5"
            stroke="currentColor"
            strokeWidth="1.75"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        Saved
      </span>
    );
  }
  return (
    <button
      type="button"
      className={`${styles.save} ${styles.saveError}`}
      onClick={() => retryLastSave()}
      title="Click to retry the last save"
    >
      <span className={styles.saveErrorLabel}>Save failed</span>
      {error && <span className={styles.saveErrorMsg}>{error}</span>}
      <span className={styles.saveRetry}>Retry</span>
    </button>
  );
}

export default ReviewPage;
