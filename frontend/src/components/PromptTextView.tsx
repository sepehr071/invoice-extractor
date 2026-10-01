import { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { patchText } from "../api/client";
import { useReview } from "../state/useReview";
import { recordLastSave } from "./lastSave";
import styles from "./PromptTextView.module.css";

type ViewMode = "view" | "edit";

function downloadBlob(filename: string, text: string, mime: string) {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

interface Props {
  text: string;
}

/**
 * PromptTextView — renders a `prompt_text` result. View mode renders the
 * markdown (GFM); Edit mode is a textarea seeded from the current text, saved
 * via PATCH /text using the shared save-status / retry plumbing. Copy and
 * Download (.md/.txt) are always available. Read-only once approved.
 */
export function PromptTextView({ text }: Props) {
  const jobId = useReview((s) => s.jobId);
  const detail = useReview((s) => s.detail);
  const setDetail = useReview((s) => s.setDetail);
  const status = detail?.status;
  const readOnly = status === "approved";

  const [mode, setMode] = useState<ViewMode>("view");
  const [draft, setDraft] = useState(text);
  const [copied, setCopied] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  // Resync the draft when the persisted text changes and we're not mid-edit.
  useEffect(() => {
    if (mode === "view") setDraft(text);
  }, [text, mode]);

  const filenameBase = (detail?.source_filename ?? "result").replace(/\.[^.]+$/, "");

  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(mode === "edit" ? draft : text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked (insecure context) — silently no-op */
    }
  };

  const save = () => {
    if (!jobId || readOnly) return;
    const next = draft;
    const run = () => {
      void (async () => {
        useReview.setState({ saveStatus: "saving", saveError: null });
        try {
          const res = await patchText(jobId, next);
          const cur = useReview.getState().detail;
          if (cur && cur.result && cur.result.kind === "prompt_text") {
            setDetail({ ...cur, result: { ...cur.result, text: res.text } });
          }
          useReview.setState({ saveStatus: "saved" });
          window.setTimeout(() => useReview.setState({ saveStatus: "idle" }), 1400);
        } catch (e) {
          useReview.setState({ saveStatus: "error", saveError: (e as Error).message });
        }
      })();
    };
    recordLastSave(run);
    run();
    setMode("view");
  };

  return (
    <div className={styles.pane}>
      <div className={styles.toolbar}>
        <div className={styles.modeGroup} role="group" aria-label="View mode">
          <button
            type="button"
            className={`${styles.modeBtn} ${mode === "view" ? styles.modeBtnActive : ""}`}
            onClick={() => setMode("view")}
            aria-pressed={mode === "view"}
          >
            Preview
          </button>
          {!readOnly && (
            <button
              type="button"
              className={`${styles.modeBtn} ${mode === "edit" ? styles.modeBtnActive : ""}`}
              onClick={() => {
                setDraft(text);
                setMode("edit");
                requestAnimationFrame(() => textareaRef.current?.focus());
              }}
              aria-pressed={mode === "edit"}
            >
              Edit
            </button>
          )}
        </div>

        <div className={styles.actions}>
          <button type="button" className={styles.actionBtn} onClick={onCopy}>
            {copied ? "Copied" : "Copy"}
          </button>
          <button
            type="button"
            className={styles.actionBtn}
            onClick={() => downloadBlob(`${filenameBase}.md`, mode === "edit" ? draft : text, "text/markdown")}
          >
            Download .md
          </button>
          <button
            type="button"
            className={styles.actionBtn}
            onClick={() => downloadBlob(`${filenameBase}.txt`, mode === "edit" ? draft : text, "text/plain")}
          >
            .txt
          </button>
        </div>
      </div>

      {readOnly && (
        <div className={styles.approvedBanner}>
          This result has been finalized and is read-only.
        </div>
      )}

      {mode === "edit" && !readOnly ? (
        <div className={styles.editWrap}>
          <textarea
            ref={textareaRef}
            className={styles.textarea}
            dir="auto"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            spellCheck={false}
            aria-label="Edit result markdown"
          />
          <div className={styles.editActions}>
            <button
              type="button"
              className={styles.cancelBtn}
              onClick={() => {
                setDraft(text);
                setMode("view");
              }}
            >
              Cancel
            </button>
            <button
              type="button"
              className={styles.saveBtn}
              onClick={save}
              disabled={draft === text}
              title={draft === text ? "No changes to save" : "Save edited text"}
            >
              Save
            </button>
          </div>
        </div>
      ) : (
        <div className={styles.markdown} dir="auto">
          {text.trim() === "" ? (
            <div className={styles.empty}>No text was found in this document.</div>
          ) : (
            <Markdown remarkPlugins={[remarkGfm]}>{text}</Markdown>
          )}
        </div>
      )}
    </div>
  );
}

export default PromptTextView;
