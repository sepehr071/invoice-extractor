import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { listJobs, uploadPdf } from "../api/client";
import type { JobSummary } from "../api/types";

const ACCEPTED_EXTENSIONS = [".pdf", ".jpg", ".jpeg", ".png", ".webp", ".bmp"];
const ACCEPT_ATTR =
  "application/pdf,.pdf,image/jpeg,image/png,image/webp,image/bmp,.jpg,.jpeg,.png,.webp,.bmp";

function isAcceptedFile(file: File): boolean {
  if (file.type === "application/pdf" || file.type.startsWith("image/")) return true;
  const name = file.name.toLowerCase();
  return ACCEPTED_EXTENSIONS.some((ext) => name.endsWith(ext));
}

export default function UploadPage() {
  const navigate = useNavigate();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [jobs, setJobs] = useState<JobSummary[]>([]);
  const [loadingJobs, setLoadingJobs] = useState(true);
  const [dragActive, setDragActive] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listJobs()
      .then((data) => {
        if (!cancelled) setJobs(data);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(`Failed to load jobs: ${err.message}`);
      })
      .finally(() => {
        if (!cancelled) setLoadingJobs(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleFile = useCallback(
    async (file: File) => {
      setError(null);
      if (!isAcceptedFile(file)) {
        setError("Only PDF or image files (JPG, PNG, WebP, BMP) are accepted.");
        return;
      }
      setUploading(true);
      try {
        const { job_id } = await uploadPdf(file);
        // Go to the Preview page so the user can draw ROIs and click Start.
        navigate(`/jobs/${job_id}/preview`);
      } catch (err) {
        setError(`Upload failed: ${(err as Error).message}`);
      } finally {
        setUploading(false);
      }
    },
    [navigate]
  );

  const onDrop = (e: React.DragEvent<HTMLLabelElement>) => {
    e.preventDefault();
    setDragActive(false);
    const file = e.dataTransfer.files?.[0];
    if (file) void handleFile(file);
  };

  const onDragOver = (e: React.DragEvent<HTMLLabelElement>) => {
    e.preventDefault();
    setDragActive(true);
  };

  const onDragLeave = (e: React.DragEvent<HTMLLabelElement>) => {
    e.preventDefault();
    setDragActive(false);
  };

  const onPick = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) void handleFile(file);
    e.target.value = "";
  };

  return (
    <div className="upload-page">
      <div className="upload-main">
        <header className="upload-hero">
          <h1>Read a document</h1>
          <p>Upload a document and get clean, readable text back. Then copy it or download it.</p>
        </header>

        <label
          className={`dropzone${dragActive ? " active" : ""}`}
          onDrop={onDrop}
          onDragOver={onDragOver}
          onDragLeave={onDragLeave}
          onClick={() => fileInputRef.current?.click()}
        >
          <span className="dropzone-icon" aria-hidden="true">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none">
              <path
                d="M12 3V15M12 3L7 8M12 3L17 8"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
              <path
                d="M4 17V19C4 20.1046 4.89543 21 6 21H18C19.1046 21 20 20.1046 20 19V17"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
              />
            </svg>
          </span>
          <h2>{uploading ? "Uploading…" : "Drop a file here"}</h2>
          <p>or click to choose one</p>
          <div className="hint">One PDF or image (JPG, PNG, WebP, BMP)</div>
          <input
            ref={fileInputRef}
            type="file"
            accept={ACCEPT_ATTR}
            onChange={onPick}
            disabled={uploading}
          />
        </label>
        {error && <div className="upload-error">{error}</div>}
      </div>

      <aside className="jobs-sidebar">
        <h3>Recent documents</h3>
        <div className="jobs-list">
          {loadingJobs ? (
            <div className="empty-state">Loading…</div>
          ) : jobs.length === 0 ? (
            <div className="empty-state">Nothing here yet. Upload a file to start.</div>
          ) : (
            jobs.map((job) => (
              <Link to={`/jobs/${job.id}`} key={job.id} className="job-row">
                <span className="filename">{job.source_filename}</span>
                <span className="meta">
                  <span className={`status-badge ${job.status}`}>{job.status}</span>
                  <span>{formatDate(job.created_at)}</span>
                </span>
              </Link>
            ))
          )}
        </div>
      </aside>
    </div>
  );
}

function formatDate(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}
