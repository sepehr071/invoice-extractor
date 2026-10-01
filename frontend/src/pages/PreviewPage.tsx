import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { getJob, getRois, pageImageUrl, startJob } from "../api/client";
import type { JobDetail, Language, Mode, OutputFormat, PageROIs, Rect } from "../api/types";
import PageNav from "../components/PageNav";
import RoiCanvas from "../components/RoiCanvas";
import styles from "./PreviewPage.module.css";

/**
 * Setup screen — shown after upload while the job is in 'created' (or 'failed',
 * for a retry) state. Built simple-first for non-technical users: one tap to
 * read the whole document, with all power-user controls (result type, invoice
 * template, language, region selection) tucked behind a "More options"
 * disclosure that the simple path never opens.
 *
 * All region/zoom/pan/RoiCanvas/startJob plumbing is preserved from the
 * original power-user console; it is only revealed when the user turns on
 * "Select parts of the page" inside More options.
 */

// Built-in instructions for the one-tap quick actions. The user never sees or
// edits these — they map a recognizable verb onto a precise instruction.
const READ_PROMPT =
  "Transcribe this document. Return all of its text, cleaned up and well formatted in markdown, preserving headings, lists, and tables in reading order. Do not summarize, omit, or add anything.";
const SUMMARIZE_PROMPT =
  "Summarize this document in clear, plain language. Cover the main points and include any important names, dates, amounts, and reference numbers.";
const KEY_INFO_PROMPT =
  "List the key information in this document as a clean markdown list: parties or names, dates, amounts, reference or ID numbers, and anything else important.";

type QuickAction = "read" | "summarize" | "key";

export default function PreviewPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();

  const [detail, setDetail] = useState<JobDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activePage, setActivePage] = useState(1);
  const [roisByPage, setRoisByPage] = useState<Record<number, Rect[]>>({});
  const [starting, setStarting] = useState(false);
  const [runningAction, setRunningAction] = useState<QuickAction | "ask" | "invoice" | null>(null);
  const [zoomPct, setZoomPct] = useState<number | null>(null); // null = fit width
  const [selectedRectIdx, setSelectedRectIdx] = useState<number | null>(null);
  const [confirmClearAll, setConfirmClearAll] = useState(false);
  const [language, setLanguage] = useState<Language>("en"); // recognizer language
  const [mode, setMode] = useState<Mode>("prompt"); // run mode — prompt is the default
  const [question, setQuestion] = useState(""); // free-form instruction
  const [outputFormat, setOutputFormat] = useState<OutputFormat>("text"); // default: reading view

  // Progressive disclosure: everything technical lives behind this toggle.
  const [moreOpen, setMoreOpen] = useState(false);
  // When on, the slim preview is swapped for the interactive region canvas.
  const [selectRegions, setSelectRegions] = useState(false);

  const imgRef = useRef<HTMLImageElement | null>(null);
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const [imgLoaded, setImgLoaded] = useState(false);
  const [displaySize, setDisplaySize] = useState<{ w: number; h: number }>({ w: 0, h: 0 });
  const [panning, setPanning] = useState(false);

  // --- load job ----------------------------------------------------------
  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    getJob(id)
      .then((d) => {
        if (cancelled) return;
        setDetail(d);
        // Setup is valid before Start ('created') and for retrying a 'failed'
        // job (re-draw regions, run again). Any other status means the pipeline
        // is running or done — bounce to the status/result page.
        if (d.status !== "created" && d.status !== "failed") {
          navigate(`/jobs/${id}`, { replace: true });
        }
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, [id, navigate]);

  // --- hydrate previously-drawn regions so they survive a reload / retry -----
  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    getRois(id)
      .then(({ rois }) => {
        if (cancelled || !rois || rois.length === 0) return;
        const byPage: Record<number, Rect[]> = {};
        for (const entry of rois) {
          const rects = (entry.rects ?? []).filter((r) => r.w > 0 && r.h > 0);
          if (rects.length > 0) byPage[entry.page] = rects;
        }
        if (Object.keys(byPage).length > 0) {
          setRoisByPage(byPage);
          // A retry with saved regions — surface the advanced surface so the
          // user sees their selection rather than wondering where it went.
          setMoreOpen(true);
          setSelectRegions(true);
        }
      })
      .catch(() => {
        /* No persisted regions yet (or fetch failed) — start with an empty canvas. */
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  const pageCount = detail?.page_count ?? 1;
  const sourceWidth = imgRef.current?.naturalWidth ?? 0;
  const sourceHeight = imgRef.current?.naturalHeight ?? 0;

  // --- image sizing ------------------------------------------------------
  const measure = useCallback(() => {
    const el = imgRef.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    setDisplaySize((prev) => (prev.w === r.width && prev.h === r.height ? prev : { w: r.width, h: r.height }));
  }, []);

  useLayoutEffect(() => {
    measure();
  }, [measure, imgLoaded, activePage, zoomPct, selectRegions]);

  useEffect(() => {
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", measure);
      return () => window.removeEventListener("resize", measure);
    }
    const el = imgRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => measure());
    ro.observe(el);
    return () => ro.disconnect();
  }, [measure, imgLoaded, selectRegions]);

  useEffect(() => {
    setImgLoaded(false);
  }, [activePage, id]);

  // Middle-mouse drag to pan a zoomed page (only meaningful while selecting
  // regions, but harmless to keep armed).
  useEffect(() => {
    const scroller = scrollerRef.current;
    if (!scroller) return;

    let active = false;
    let startX = 0;
    let startY = 0;
    let scrollStartX = 0;
    let scrollStartY = 0;
    let pointerId = -1;

    const stop = () => {
      if (!active) return;
      active = false;
      setPanning(false);
      try {
        if (pointerId !== -1) scroller.releasePointerCapture(pointerId);
      } catch {
        /* noop */
      }
      pointerId = -1;
    };

    const onPointerDown = (e: PointerEvent) => {
      if (e.button !== 1) return; // wheel-button only
      e.preventDefault();
      active = true;
      pointerId = e.pointerId;
      startX = e.clientX;
      startY = e.clientY;
      scrollStartX = scroller.scrollLeft;
      scrollStartY = scroller.scrollTop;
      setPanning(true);
      try {
        scroller.setPointerCapture(e.pointerId);
      } catch {
        /* noop */
      }
    };

    const onPointerMove = (e: PointerEvent) => {
      if (!active) return;
      e.preventDefault();
      scroller.scrollLeft = scrollStartX - (e.clientX - startX);
      scroller.scrollTop = scrollStartY - (e.clientY - startY);
    };

    const onAuxClick = (e: MouseEvent) => {
      if (e.button === 1) e.preventDefault();
    };

    scroller.addEventListener("pointerdown", onPointerDown);
    scroller.addEventListener("pointermove", onPointerMove);
    scroller.addEventListener("pointerup", stop);
    scroller.addEventListener("pointercancel", stop);
    scroller.addEventListener("pointerleave", stop);
    scroller.addEventListener("auxclick", onAuxClick);
    return () => {
      scroller.removeEventListener("pointerdown", onPointerDown);
      scroller.removeEventListener("pointermove", onPointerMove);
      scroller.removeEventListener("pointerup", stop);
      scroller.removeEventListener("pointercancel", stop);
      scroller.removeEventListener("pointerleave", stop);
      scroller.removeEventListener("auxclick", onAuxClick);
      stop();
    };
  }, [selectRegions]);

  // Selection is page-local — drop it whenever the active page changes.
  useEffect(() => {
    setSelectedRectIdx(null);
  }, [activePage]);

  // --- region editing ----------------------------------------------------
  const currentRects = useMemo(() => roisByPage[activePage] ?? [], [roisByPage, activePage]);

  const setCurrentRects = (next: Rect[]) => {
    setRoisByPage((prev) => ({ ...prev, [activePage]: next }));
  };

  const clearCurrentPage = () => {
    setSelectedRectIdx(null);
    setRoisByPage((prev) => {
      const { [activePage]: _drop, ...rest } = prev;
      return rest;
    });
  };

  const deleteSelectedRect = () => {
    if (selectedRectIdx === null) return;
    setCurrentRects(currentRects.filter((_, i) => i !== selectedRectIdx));
    setSelectedRectIdx(null);
  };

  const clearAll = () => {
    if (Object.keys(roisByPage).length === 0) return;
    setConfirmClearAll(true);
  };

  const confirmClearAllNow = () => {
    setRoisByPage({});
    setSelectedRectIdx(null);
    setConfirmClearAll(false);
  };

  const totalRects = Object.values(roisByPage).reduce((acc, r) => acc + r.length, 0);
  const pagesWithRects = Object.keys(roisByPage).filter((k) => (roisByPage[Number(k)] ?? []).length > 0).length;

  // --- start -------------------------------------------------------------
  const collectRois = (): PageROIs[] =>
    Object.entries(roisByPage)
      .map(([page, rects]) => ({ page: Number(page), rects: rects.filter((r) => r.w > 0 && r.h > 0) }))
      .filter((entry) => entry.rects.length > 0);

  const run = async (
    label: QuickAction | "ask" | "invoice",
    opts:
      | { mode: "prompt"; prompt: string; output_format: OutputFormat }
      | { mode: "invoice" }
  ) => {
    if (!id || !detail || starting) return;
    setStarting(true);
    setRunningAction(label);
    setError(null);
    try {
      const rois = collectRois();
      await startJob(id, { rois, language, ...opts });
      navigate(`/jobs/${id}`);
    } catch (err) {
      setError((err as Error).message);
      setStarting(false);
      setRunningAction(null);
    }
  };

  const runQuick = (action: QuickAction) => {
    const prompt =
      action === "read" ? READ_PROMPT : action === "summarize" ? SUMMARIZE_PROMPT : KEY_INFO_PROMPT;
    void run(action, { mode: "prompt", prompt, output_format: "text" });
  };

  const runAsk = () => {
    const trimmed = question.trim();
    if (trimmed === "") return;
    void run("ask", { mode: "prompt", prompt: trimmed, output_format: outputFormat });
  };

  const runInvoice = () => {
    void run("invoice", { mode: "invoice" });
  };

  // --- guards / early returns --------------------------------------------
  if (!id) {
    return (
      <div className={styles.page}>
        <div className={styles.errorCard}>
          Missing job id. <Link to="/">Back to start</Link>
        </div>
      </div>
    );
  }
  if (error && !detail) {
    return (
      <div className={styles.page}>
        <div className={styles.errorCard}>
          <p>We could not open that document: {error}</p>
          <Link to="/">Back to start</Link>
        </div>
      </div>
    );
  }
  if (!detail) {
    return (
      <div className={styles.page}>
        <div className={styles.loading}>Loading…</div>
      </div>
    );
  }

  const imageStyle: React.CSSProperties =
    zoomPct === null
      ? { width: "100%", maxWidth: "100%", height: "auto" }
      : sourceWidth > 0
      ? { width: `${(sourceWidth * zoomPct) / 100}px`, height: "auto", maxWidth: "none" }
      : { width: `${zoomPct}%` };

  const askEmpty = question.trim() === "";

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <Link to="/" className={styles.backLink}>
          ← Back
        </Link>
        <h1 className={styles.filename} title={detail.source_filename}>
          {detail.source_filename}
        </h1>
        {detail.page_count != null && (
          <span className={styles.pageMeta}>
            {detail.page_count} page{detail.page_count === 1 ? "" : "s"}
          </span>
        )}
      </header>

      <div className={styles.body}>
        {/* LEFT — the document preview. Non-interactive by default; becomes the
            region-selection canvas only when the user turns it on. */}
        <section className={styles.previewCol} aria-label="Document preview">
          {selectRegions ? (
            <div className={styles.selectStage}>
              <div className={styles.selectBar}>
                <div className={styles.zoomGroup} role="group" aria-label="Zoom">
                  <button
                    type="button"
                    className={styles.iconBtn}
                    onClick={() => setZoomPct((z) => Math.max(25, (z ?? 100) - 10))}
                    title="Zoom out"
                  >
                    −
                  </button>
                  <button
                    type="button"
                    className={`${styles.iconBtn} ${zoomPct === null ? styles.iconBtnActive : ""}`}
                    onClick={() => setZoomPct(null)}
                    title="Fit width"
                  >
                    Fit
                  </button>
                  <button
                    type="button"
                    className={`${styles.iconBtn} ${zoomPct === 100 ? styles.iconBtnActive : ""}`}
                    onClick={() => setZoomPct(100)}
                    title="Actual size"
                  >
                    100%
                  </button>
                  <button
                    type="button"
                    className={styles.iconBtn}
                    onClick={() => setZoomPct((z) => Math.min(400, (z ?? 100) + 10))}
                    title="Zoom in"
                  >
                    +
                  </button>
                </div>
                <div className={styles.selectActions}>
                  {selectedRectIdx !== null && !starting && (
                    <button
                      type="button"
                      className={styles.dangerBtn}
                      onClick={deleteSelectedRect}
                      title="Remove the selected area (Del)"
                    >
                      Remove area
                    </button>
                  )}
                  <button
                    type="button"
                    className={styles.quietBtn}
                    onClick={clearCurrentPage}
                    disabled={currentRects.length === 0 || starting}
                  >
                    Clear page
                  </button>
                  {confirmClearAll ? (
                    <span className={styles.inlineConfirm}>
                      <span className={styles.inlineConfirmText}>Clear every area?</span>
                      <button type="button" className={styles.confirmYesBtn} onClick={confirmClearAllNow}>
                        Clear all
                      </button>
                      <button
                        type="button"
                        className={styles.confirmNoBtn}
                        onClick={() => setConfirmClearAll(false)}
                      >
                        Cancel
                      </button>
                    </span>
                  ) : (
                    <button
                      type="button"
                      className={styles.quietBtn}
                      onClick={clearAll}
                      disabled={totalRects === 0 || starting}
                    >
                      Clear all
                    </button>
                  )}
                </div>
              </div>

              <p className={styles.selectHint}>
                Drag on the page to mark the parts you want read. Click an area to select it, then
                press <kbd>Del</kbd> to remove it. Pages you do not mark are read in full.
              </p>

              <div className={`${styles.scroller} ${panning ? styles.panning : ""}`} ref={scrollerRef}>
                <div className={styles.stage}>
                  <div className={styles.canvas}>
                    <img
                      ref={imgRef}
                      key={`${id}-${activePage}`}
                      src={pageImageUrl(id, activePage, "preview")}
                      alt={`Page ${activePage}`}
                      className={styles.image}
                      style={imageStyle}
                      draggable={false}
                      onLoad={() => setImgLoaded(true)}
                    />
                    {imgLoaded && displaySize.w > 0 && displaySize.h > 0 && sourceWidth > 0 && (
                      <RoiCanvas
                        sourceWidth={sourceWidth}
                        sourceHeight={sourceHeight}
                        displayWidth={displaySize.w}
                        displayHeight={displaySize.h}
                        rects={currentRects}
                        onChange={setCurrentRects}
                        disabled={starting}
                        selectedIndex={selectedRectIdx}
                        onSelectedIndexChange={setSelectedRectIdx}
                      />
                    )}
                  </div>
                </div>
              </div>

              {pageCount > 1 && (
                <PageNav currentPage={activePage} pageCount={pageCount} onChange={setActivePage} />
              )}

              <div className={styles.selectSummary}>
                {totalRects === 0
                  ? "No areas marked. The whole document will be read."
                  : `${totalRects} area${totalRects === 1 ? "" : "s"} on ${pagesWithRects} page${
                      pagesWithRects === 1 ? "" : "s"
                    }.`}
              </div>
            </div>
          ) : (
            <div className={styles.thumbWrap}>
              <div className={styles.thumb}>
                <img
                  ref={imgRef}
                  key={`${id}-thumb-${activePage}`}
                  src={pageImageUrl(id, activePage, "preview")}
                  alt={`Page ${activePage} of your document`}
                  className={styles.thumbImg}
                  draggable={false}
                  onLoad={() => setImgLoaded(true)}
                />
              </div>
              {pageCount > 1 && (
                <PageNav currentPage={activePage} pageCount={pageCount} onChange={setActivePage} />
              )}
            </div>
          )}
        </section>

        {/* RIGHT — the hero of this screen. */}
        <section className={styles.actionCol} aria-label="What do you need">
          <h2 className={styles.actionTitle}>What do you need?</h2>

          {mode === "invoice" ? (
            <div className={styles.invoicePanel}>
              <p className={styles.invoiceLead}>
                Reads this as a trade invoice and pulls out structured fields and line items for
                review.
              </p>
              <button
                type="button"
                className={styles.primaryBtn}
                onClick={runInvoice}
                disabled={starting}
              >
                {runningAction === "invoice" ? "Starting…" : "Start invoice extraction"}
              </button>
            </div>
          ) : (
            <>
              <p className={styles.actionLead}>
                Pick one and we will get to work. The most common choice is reading the whole
                document.
              </p>

              <button
                type="button"
                className={styles.primaryBtn}
                onClick={() => runQuick("read")}
                disabled={starting}
              >
                {runningAction === "read" ? "Starting…" : "Read all the text"}
              </button>

              <div className={styles.secondaryRow}>
                <button
                  type="button"
                  className={styles.secondaryBtn}
                  onClick={() => runQuick("summarize")}
                  disabled={starting}
                >
                  {runningAction === "summarize" ? "Starting…" : "Summarize"}
                </button>
                <button
                  type="button"
                  className={styles.secondaryBtn}
                  onClick={() => runQuick("key")}
                  disabled={starting}
                >
                  {runningAction === "key" ? "Starting…" : "Find key info"}
                </button>
              </div>

              <div className={styles.askDivider}>
                <span>or ask for something specific</span>
              </div>

              <textarea
                className={styles.askBox}
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                onKeyDown={(e) => {
                  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                    e.preventDefault();
                    runAsk();
                  }
                }}
                disabled={starting}
                rows={3}
                placeholder="e.g. what is the total and the due date?"
                aria-label="Ask for something specific"
              />
              <button
                type="button"
                className={styles.askBtn}
                onClick={runAsk}
                disabled={starting || askEmpty}
              >
                {runningAction === "ask" ? "Starting…" : "Get result"}
              </button>
            </>
          )}

          {/* Progressive disclosure — collapsed by default, never a modal. */}
          <div className={styles.more}>
            <button
              type="button"
              className={styles.moreToggle}
              onClick={() => setMoreOpen((v) => !v)}
              aria-expanded={moreOpen}
              aria-controls="more-options"
            >
              <span className={`${styles.chev} ${moreOpen ? styles.chevOpen : ""}`} aria-hidden="true">
                ›
              </span>
              More options
            </button>

            {moreOpen && (
              <div id="more-options" className={styles.moreBody}>
                {mode === "prompt" && (
                  <div className={styles.optRow}>
                    <span className={styles.optLabel}>Result type</span>
                    <div className={styles.segmented} role="group" aria-label="Result type">
                      <button
                        type="button"
                        className={`${styles.segBtn} ${outputFormat === "text" ? styles.segBtnActive : ""}`}
                        onClick={() => setOutputFormat("text")}
                        disabled={starting}
                        aria-pressed={outputFormat === "text"}
                      >
                        Reading view
                      </button>
                      <button
                        type="button"
                        className={`${styles.segBtn} ${outputFormat === "json" ? styles.segBtnActive : ""}`}
                        onClick={() => setOutputFormat("json")}
                        disabled={starting}
                        aria-pressed={outputFormat === "json"}
                      >
                        Structured fields
                      </button>
                    </div>
                  </div>
                )}

                <div className={styles.optRow}>
                  <span className={styles.optLabel}>Invoice template</span>
                  <div className={styles.segmented} role="group" aria-label="Invoice template">
                    <button
                      type="button"
                      className={`${styles.segBtn} ${mode === "prompt" ? styles.segBtnActive : ""}`}
                      onClick={() => setMode("prompt")}
                      disabled={starting}
                      aria-pressed={mode === "prompt"}
                    >
                      Off
                    </button>
                    <button
                      type="button"
                      className={`${styles.segBtn} ${mode === "invoice" ? styles.segBtnActive : ""}`}
                      onClick={() => setMode("invoice")}
                      disabled={starting}
                      aria-pressed={mode === "invoice"}
                    >
                      On
                    </button>
                  </div>
                </div>

                <div className={styles.optRow}>
                  <span className={styles.optLabel}>Document language</span>
                  <div className={styles.segmented} role="group" aria-label="Document language">
                    <button
                      type="button"
                      className={`${styles.segBtn} ${language === "en" ? styles.segBtnActive : ""}`}
                      onClick={() => setLanguage("en")}
                      disabled={starting}
                      aria-pressed={language === "en"}
                    >
                      English
                    </button>
                    <button
                      type="button"
                      className={`${styles.segBtn} ${language === "fa" ? styles.segBtnActive : ""}`}
                      onClick={() => setLanguage("fa")}
                      disabled={starting}
                      aria-pressed={language === "fa"}
                    >
                      فارسی / Persian
                    </button>
                  </div>
                </div>

                <div className={styles.optRow}>
                  <span className={styles.optLabel}>Select parts of the page</span>
                  <div className={styles.segmented} role="group" aria-label="Select parts of the page">
                    <button
                      type="button"
                      className={`${styles.segBtn} ${!selectRegions ? styles.segBtnActive : ""}`}
                      onClick={() => setSelectRegions(false)}
                      disabled={starting}
                      aria-pressed={!selectRegions}
                    >
                      Off
                    </button>
                    <button
                      type="button"
                      className={`${styles.segBtn} ${selectRegions ? styles.segBtnActive : ""}`}
                      onClick={() => setSelectRegions(true)}
                      disabled={starting}
                      aria-pressed={selectRegions}
                    >
                      On
                    </button>
                  </div>
                </div>
                {selectRegions && (
                  <p className={styles.optNote}>
                    Mark areas on the document at the left. Anything you do not mark is read in full.
                  </p>
                )}
              </div>
            )}
          </div>

          {error && <div className={styles.inlineError}>{error}</div>}
        </section>
      </div>
    </div>
  );
}
