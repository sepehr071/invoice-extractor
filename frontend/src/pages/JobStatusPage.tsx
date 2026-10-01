import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { getJob } from "../api/client";
import type { JobDetail, JobStatus } from "../api/types";
import { useReview } from "../state/useReview";
import ReviewPage from "./ReviewPage";

const POLL_MS = 1500;

const PHASES: { key: JobStatus; label: string; detail: string }[] = [
  { key: "queued", label: "Getting ready", detail: "Lining up your document" },
  { key: "ocr", label: "Reading", detail: "Reading the text from your document" },
  { key: "extract", label: "Understanding", detail: "Pulling out what you asked for" },
  { key: "linking", label: "Finishing", detail: "Tidying up the result" },
];

// Inline styles for the failed-state actions and stuck hint. JobStatusPage uses
// global App.css classes (not a CSS module) and App.css is owned by another agent,
// so new affordances are styled locally to avoid cross-file edits.
const ACTION_ROW_STYLE: React.CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  alignItems: "center",
  gap: 10,
  marginTop: 16,
};
const PRIMARY_BTN_STYLE: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  padding: "7px 14px",
  borderRadius: 8,
  background: "#2563eb",
  color: "#fff",
  fontSize: 13,
  fontWeight: 600,
  textDecoration: "none",
};
const SECONDARY_BTN_STYLE: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  padding: "7px 14px",
  borderRadius: 8,
  background: "#fff",
  color: "#374151",
  border: "1px solid #d1d5db",
  fontSize: 13,
  fontWeight: 500,
  textDecoration: "none",
};
const LINK_BTN_STYLE: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  padding: "7px 6px",
  color: "#6b7280",
  fontSize: 13,
  textDecoration: "none",
};
const RETRY_HINT_STYLE: React.CSSProperties = {
  margin: "12px 0 0",
  fontSize: 12,
  color: "#6b7280",
  lineHeight: 1.45,
};
const STUCK_HINT_STYLE: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 10,
  flexWrap: "wrap",
  marginTop: 14,
  padding: "10px 12px",
  background: "rgba(245, 158, 11, 0.1)",
  border: "1px solid rgba(245, 158, 11, 0.35)",
  borderRadius: 8,
  color: "#92400e",
  fontSize: 12,
  lineHeight: 1.45,
};
const REFRESH_BTN_STYLE: React.CSSProperties = {
  marginLeft: "auto",
  padding: "5px 12px",
  borderRadius: 6,
  border: "1px solid #d97706",
  background: "#fff",
  color: "#b45309",
  fontSize: 12,
  fontWeight: 600,
  cursor: "pointer",
};
// Reassuring one-liner above the phase detail. Styled inline because App.css is
// owned by another agent; this keeps the change inside JobStatusPage.
const LEAD_STYLE: React.CSSProperties = {
  margin: "0 0 10px",
  fontSize: 16,
  fontWeight: 600,
  letterSpacing: "-0.01em",
  color: "#111827",
};

function isTerminal(status: JobStatus): boolean {
  return status === "ready" || status === "approved" || status === "failed";
}

function phaseIndex(status: JobStatus): number {
  const idx = PHASES.findIndex((p) => p.key === status);
  if (idx >= 0) return idx;
  return status === "ready" || status === "approved" ? PHASES.length : -1;
}

/** No observed progress for this long → surface a "taking longer than usual" hint. */
const STUCK_AFTER_MS = 90_000;

export default function JobStatusPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [detail, setDetailLocal] = useState<JobDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stuck, setStuck] = useState(false);
  const setJobId = useReview((s) => s.setJobId);
  const setStoreDetail = useReview((s) => s.setDetail);
  const intervalRef = useRef<number | null>(null);
  // Track the last (status, progress_pct) we saw and when it last changed.
  const progressMarkerRef = useRef<{ key: string; at: number } | null>(null);
  const stuckTimerRef = useRef<number | null>(null);

  const refreshNow = useCallback(async () => {
    if (!id) return;
    try {
      const d = await getJob(id);
      setDetailLocal(d);
      setStoreDetail(d);
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    }
  }, [id, setStoreDetail]);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    setJobId(id);
    setDetailLocal(null);
    setError(null);
    setStuck(false);
    progressMarkerRef.current = null;

    const armStuckTimer = () => {
      if (stuckTimerRef.current != null) window.clearTimeout(stuckTimerRef.current);
      stuckTimerRef.current = window.setTimeout(() => {
        if (!cancelled) setStuck(true);
      }, STUCK_AFTER_MS);
    };

    const tick = async () => {
      try {
        const d = await getJob(id);
        if (cancelled) return;
        // Pre-start state: bounce to Preview so user can draw ROIs.
        if (d.status === "created") {
          navigate(`/jobs/${id}/preview`, { replace: true });
          return;
        }
        setDetailLocal(d);
        setStoreDetail(d);

        // Stuck-detection: reset the timer whenever status or progress advances.
        const marker = `${d.status}:${d.progress_pct}`;
        if (!progressMarkerRef.current || progressMarkerRef.current.key !== marker) {
          progressMarkerRef.current = { key: marker, at: Date.now() };
          setStuck(false);
          if (!isTerminal(d.status)) armStuckTimer();
        }

        if (isTerminal(d.status)) {
          if (intervalRef.current != null) {
            window.clearInterval(intervalRef.current);
            intervalRef.current = null;
          }
          if (stuckTimerRef.current != null) {
            window.clearTimeout(stuckTimerRef.current);
            stuckTimerRef.current = null;
          }
          setStuck(false);
        }
      } catch (err) {
        if (!cancelled) setError((err as Error).message);
      }
    };

    void tick();
    intervalRef.current = window.setInterval(tick, POLL_MS);
    armStuckTimer();

    return () => {
      cancelled = true;
      if (intervalRef.current != null) {
        window.clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
      if (stuckTimerRef.current != null) {
        window.clearTimeout(stuckTimerRef.current);
        stuckTimerRef.current = null;
      }
    };
  }, [id, navigate, setJobId, setStoreDetail]);

  if (!id) {
    return (
      <div className="status-page">
        <div className="status-card">
          <h2>We could not find that document</h2>
          <Link to="/" className="status-back">
            ← Back to start
          </Link>
        </div>
      </div>
    );
  }

  if (error && !detail) {
    return (
      <div className="status-page">
        <div className="status-card">
          <h2>We could not open that document</h2>
          <div className="status-error">{error}</div>
          <Link to="/" className="status-back">
            ← Back to start
          </Link>
        </div>
      </div>
    );
  }

  if (!detail) {
    return (
      <div className="status-page">
        <div className="status-card status-loading">
          <div className="status-spinner" aria-hidden="true" />
          <h2>Loading…</h2>
        </div>
      </div>
    );
  }

  if (detail.status === "ready" || detail.status === "approved") {
    return <ReviewPage />;
  }

  if (detail.status === "failed") {
    return (
      <div className="status-page">
        <div className="status-card">
          <h2>Something went wrong</h2>
          <p className="phase">{detail.source_filename}</p>
          {detail.error && <div className="status-error">{detail.error}</div>}
          <div style={ACTION_ROW_STYLE}>
            <Link to={`/jobs/${id}/preview`} style={PRIMARY_BTN_STYLE}>
              Try again
            </Link>
            <Link to="/" style={LINK_BTN_STYLE}>
              ← Back to start
            </Link>
          </div>
          <p style={RETRY_HINT_STYLE}>
            Try again takes you back to the setup screen with your choices kept, so you can adjust
            them before running again.
          </p>
        </div>
      </div>
    );
  }

  const currentIdx = phaseIndex(detail.status);
  const phaseInfo = PHASES[currentIdx] ?? PHASES[0];

  return (
    <div className="status-page">
      <div className="status-card">
        <div className="status-head">
          <Link to="/" className="status-back-inline">
            ← Back
          </Link>
          <h2 className="status-filename" title={detail.source_filename}>
            {detail.source_filename}
          </h2>
        </div>

        <p style={LEAD_STYLE}>Reading your document…</p>

        <p className="status-phase">
          <span className="status-phase-pulse" aria-hidden="true" />
          {phaseInfo.label} <span className="status-phase-detail">· {phaseInfo.detail}</span>
        </p>

        <div className="progress-track">
          <div
            className="progress-bar"
            style={{ width: `${Math.max(0, Math.min(100, detail.progress_pct))}%` }}
          />
        </div>

        <ol className="phase-list" aria-label="Pipeline phases">
          {PHASES.map((p, i) => {
            const done = i < currentIdx;
            const active = i === currentIdx;
            return (
              <li
                key={p.key}
                className={`phase-step ${done ? "done" : ""} ${active ? "active" : ""}`}
              >
                <span className="phase-marker">
                  {done ? "✓" : active ? <span className="phase-marker-pulse" /> : i + 1}
                </span>
                <span className="phase-label">{p.label}</span>
              </li>
            );
          })}
        </ol>

        <div className="status-foot">
          <span>{detail.progress_pct}% complete</span>
          {detail.page_count != null && (
            <span>
              · {detail.page_count} page{detail.page_count === 1 ? "" : "s"}
            </span>
          )}
        </div>

        {stuck && (
          <div style={STUCK_HINT_STYLE} role="status">
            <span>
              This is taking a little longer than usual. Long or busy documents can take more
              time, and we are still working on it.
            </span>
            <button type="button" style={REFRESH_BTN_STYLE} onClick={() => void refreshNow()}>
              Refresh now
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
