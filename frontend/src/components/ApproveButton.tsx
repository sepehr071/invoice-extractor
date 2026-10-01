import { useState } from "react";
import { useReview } from "../state/useReview";
import type { JobResult } from "../api/types";
import styles from "./ApproveButton.module.css";

/** Build the export payload + filename for the current result variant. */
function exportSpec(
  result: JobResult,
  filenameBase: string
): { filename: string; text: string; mime: string } {
  switch (result.kind) {
    case "invoice":
      return {
        filename: `${filenameBase}.json`,
        text: JSON.stringify(result.invoice, null, 2),
        mime: "application/json",
      };
    case "prompt_json":
      return {
        filename: `${filenameBase}.json`,
        text: JSON.stringify(result.data, null, 2),
        mime: "application/json",
      };
    case "prompt_text":
      return {
        filename: `${filenameBase}.md`,
        text: result.text,
        mime: "text/markdown",
      };
  }
}

export function ApproveButton() {
  const status = useReview((s) => s.detail?.status);
  const detail = useReview((s) => s.detail);
  const approve = useReview((s) => s.approve);

  const [busy, setBusy] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);

  const result = detail?.result ?? null;
  const isPrompt = detail?.mode === "prompt";
  const filenameBase = (detail?.source_filename ?? "result").replace(/\.[^.]+$/, "");

  const doExport = () => {
    if (!result) return;
    const { filename, text, mime } = exportSpec(result, filenameBase);
    const blob = new Blob([text], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  };

  // Already finalized → offer a re-export of the persisted payload.
  if (status === "approved") {
    return (
      <div className={styles.wrap}>
        <button
          type="button"
          className={styles.reexportBtn}
          onClick={doExport}
          disabled={!result}
          title={result ? "Download the finalized export" : "Result not loaded yet"}
        >
          <svg width="13" height="13" viewBox="0 0 14 14" fill="none">
            <path
              d="M7 1.5V9.5M7 9.5L3.5 6M7 9.5L10.5 6M2 12H12"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          Re-export
        </button>
      </div>
    );
  }

  if (status !== "ready") {
    return null;
  }

  const runApprove = async () => {
    if (busy) return;
    setBusy(true);
    setServerError(null);
    try {
      await approve();
    } catch (err) {
      setServerError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={styles.wrap}>
      <div className={styles.row}>
        <button
          type="button"
          className={styles.approveBtn}
          onClick={() => void runApprove()}
          disabled={busy}
          title={isPrompt ? "Finalize and export the result" : "Approve & export the invoice"}
        >
          {busy ? "Finalizing…" : isPrompt ? "Finalize & Export" : "Approve & Export"}
        </button>
      </div>

      {serverError && <div className={styles.inlineError}>{serverError}</div>}
    </div>
  );
}

export default ApproveButton;
