import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useReview } from "../state/useReview";
import { pageImageUrl } from "../api/client";
import type { FieldLink, JobResult, LineItemLink, OcrToken, PageInfo } from "../api/types";
import { BboxOverlay } from "./BboxOverlay";
import { PageNav } from "./PageNav";
import styles from "./PdfPane.module.css";

type ZoomMode = "fit" | { kind: "abs"; pct: number };

const ZOOM_STEPS = [50, 75, 100, 125, 150, 200];
const ZOOM_MIN = 25;
const ZOOM_MAX = 400;
const WHEEL_STEP = 0.12; // ~12% per wheel tick

interface PdfPaneProps {
  /** Render bbox overlays + capture click-to-link. Off for prompt_text mode. */
  overlays?: boolean;
}

/** Union-safe view of a JobResult: every kind exposes `pages`; only some carry
 *  links. Defaults to empty arrays so the pane never branches on `kind`. */
function readResult(result: JobResult | null): {
  fields: FieldLink[];
  lineItems: LineItemLink[];
  pages: PageInfo[];
} {
  if (!result) return { fields: [], lineItems: [], pages: [] };
  if (result.kind === "invoice") {
    return { fields: result.fields, lineItems: result.line_items, pages: result.pages };
  }
  if (result.kind === "prompt_json") {
    return { fields: result.fields, lineItems: [], pages: result.pages };
  }
  return { fields: [], lineItems: [], pages: result.pages };
}

export function PdfPane({ overlays = true }: PdfPaneProps) {
  const jobId = useReview((s) => s.jobId);
  const detail = useReview((s) => s.detail);
  const ocr = useReview((s) => s.ocr);
  const activePage = useReview((s) => s.activePage);
  const activeFieldPath = useReview((s) => s.activeFieldPath);
  const hoverFieldPath = useReview((s) => s.hoverFieldPath);
  const setActiveField = useReview((s) => s.setActiveField);
  const setActivePage = useReview((s) => s.setActivePage);

  const imgRef = useRef<HTMLImageElement | null>(null);
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const [displaySize, setDisplaySize] = useState<{ w: number; h: number }>({
    w: 0,
    h: 0,
  });
  const [imgLoaded, setImgLoaded] = useState(false);
  const [zoom, setZoom] = useState<ZoomMode>("fit");

  const { fields, lineItems, pages } = readResult(detail?.result ?? null);

  const pageInfo = pages.find((p) => p.page === activePage) ?? pages[0] ?? null;
  const sourceWidth = pageInfo?.width ?? 0;
  const sourceHeight = pageInfo?.height ?? 0;

  const ocrTokens: OcrToken[] =
    ocr?.pages.find((p) => p.page === activePage)?.tokens ?? [];

  const measure = useCallback(() => {
    const el = imgRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    setDisplaySize((prev) =>
      prev.w === rect.width && prev.h === rect.height
        ? prev
        : { w: rect.width, h: rect.height }
    );
  }, []);

  useLayoutEffect(() => {
    measure();
  }, [measure, imgLoaded, activePage, pageInfo?.image_url, zoom]);

  useEffect(() => {
    const el = imgRef.current;
    if (!el) return;
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", measure);
      return () => window.removeEventListener("resize", measure);
    }
    const ro = new ResizeObserver(() => measure());
    ro.observe(el);
    return () => ro.disconnect();
  }, [measure, imgLoaded]);

  useEffect(() => {
    setImgLoaded(false);
  }, [activePage, jobId]);

  const pageCount = pages.length || detail?.page_count || 1;
  const handlePageChange = useCallback(
    (n: number) => setActivePage(Math.max(1, Math.min(pageCount, n))),
    [pageCount, setActivePage]
  );

  const zoomPct = zoom === "fit" ? null : zoom.pct;

  const zoomIn = () => {
    const cur = zoomPct ?? 100;
    const next = ZOOM_STEPS.find((s) => s > cur) ?? ZOOM_STEPS[ZOOM_STEPS.length - 1];
    setZoom({ kind: "abs", pct: next });
  };
  const zoomOut = () => {
    const cur = zoomPct ?? 100;
    const next = [...ZOOM_STEPS].reverse().find((s) => s < cur) ?? ZOOM_STEPS[0];
    setZoom({ kind: "abs", pct: next });
  };

  // Middle-mouse (wheel-button) drag → pan the scroller. Active during
  // a single press; releases on pointerup / leave.
  const [panning, setPanning] = useState(false);
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
        /* pointer may already be released */
      }
      pointerId = -1;
    };

    const onPointerDown = (e: PointerEvent) => {
      // Middle button (1) only — left button is reserved for field selection.
      if (e.button !== 1) return;
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
        /* capture may fail on non-pointer-capture browsers; deltas still work */
      }
    };

    const onPointerMove = (e: PointerEvent) => {
      if (!active) return;
      e.preventDefault();
      scroller.scrollLeft = scrollStartX - (e.clientX - startX);
      scroller.scrollTop = scrollStartY - (e.clientY - startY);
    };

    // Suppress the browser's middle-click autoscroll cursor entirely.
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
  }, []);

  // Ctrl/Cmd + wheel → zoom anchored on cursor position.
  useEffect(() => {
    const scroller = scrollerRef.current;
    if (!scroller) return;

    const onWheel = (e: WheelEvent) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      e.preventDefault();

      const img = imgRef.current;
      if (!img || sourceWidth <= 0) return;

      const imgRect = img.getBoundingClientRect();
      const scrRect = scroller.getBoundingClientRect();

      // Cursor in viewport
      const cursorXInScroller = e.clientX - scrRect.left;
      const cursorYInScroller = e.clientY - scrRect.top;

      // Current zoom factor (rendered pixels per source pixel)
      const currentZoomFactor = imgRect.width / sourceWidth;
      const currentPct = currentZoomFactor * 100;

      // Cursor in source-pixel coordinates of the page
      const cursorXOnSource = (e.clientX - imgRect.left) / currentZoomFactor;
      const cursorYOnSource = (e.clientY - imgRect.top) / currentZoomFactor;

      // New zoom
      const direction = e.deltaY < 0 ? 1 : -1;
      const newPct = Math.max(
        ZOOM_MIN,
        Math.min(ZOOM_MAX, currentPct * (1 + direction * WHEEL_STEP))
      );
      if (Math.abs(newPct - currentPct) < 0.1) return;

      setZoom({ kind: "abs", pct: Math.round(newPct) });

      // After React re-renders the new image size, read the new image position
      // and adjust scroll so the cursor still sits over (cursorXOnSource, cursorYOnSource).
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          const img2 = imgRef.current;
          const sc2 = scrollerRef.current;
          if (!img2 || !sc2) return;
          const newImgRect = img2.getBoundingClientRect();
          const newScrRect = sc2.getBoundingClientRect();
          const newZoomFactor = newImgRect.width / sourceWidth;

          // Where the source point now sits in viewport (with current scroll):
          const newCursorViewportX =
            newImgRect.left + cursorXOnSource * newZoomFactor;
          const newCursorViewportY =
            newImgRect.top + cursorYOnSource * newZoomFactor;

          // Delta to put it back under the original cursor viewport position:
          const dx = newCursorViewportX - (newScrRect.left + cursorXInScroller);
          const dy = newCursorViewportY - (newScrRect.top + cursorYInScroller);

          sc2.scrollLeft += dx;
          sc2.scrollTop += dy;
        });
      });
    };

    scroller.addEventListener("wheel", onWheel, { passive: false });
    return () => scroller.removeEventListener("wheel", onWheel);
  }, [sourceWidth, sourceHeight]);

  if (!jobId || !detail) return <div className={styles.placeholder}>Loading…</div>;
  if (!pageInfo) return <div className={styles.placeholder}>No pages available.</div>;

  const imageStyle: React.CSSProperties =
    zoom === "fit"
      ? { width: "100%", maxWidth: "100%", height: "auto" }
      : { width: `${(sourceWidth * zoom.pct) / 100}px`, height: "auto", maxWidth: "none" };

  return (
    <div className={styles.pane}>
      <div className={styles.toolbar}>
        <div className={styles.zoomGroup} role="group" aria-label="Zoom">
          <button
            type="button"
            className={styles.iconBtn}
            onClick={zoomOut}
            disabled={zoomPct !== null && zoomPct <= ZOOM_STEPS[0]}
            title="Zoom out (-)"
          >
            <span aria-hidden="true">−</span>
          </button>
          <button
            type="button"
            className={`${styles.iconBtn} ${zoom === "fit" ? styles.iconBtnActive : ""}`}
            onClick={() => setZoom("fit")}
            title="Fit width"
          >
            Fit
          </button>
          <button
            type="button"
            className={`${styles.iconBtn} ${zoomPct === 100 ? styles.iconBtnActive : ""}`}
            onClick={() => setZoom({ kind: "abs", pct: 100 })}
            title="100%"
          >
            100%
          </button>
          <button
            type="button"
            className={styles.iconBtn}
            onClick={zoomIn}
            disabled={zoomPct !== null && zoomPct >= ZOOM_STEPS[ZOOM_STEPS.length - 1]}
            title="Zoom in (+)"
          >
            <span aria-hidden="true">+</span>
          </button>
          {zoom !== "fit" && zoom.pct !== 100 && (
            <span className={styles.zoomReadout}>{zoom.pct}%</span>
          )}
        </div>

        <div className={styles.legend}>
          <span className={styles.legendDot} data-kind="hover" /> Hover
          <span className={styles.legendDot} data-kind="active" /> Selected
        </div>
      </div>

      <div
        className={`${styles.scroller} ${panning ? styles.panning : ""}`}
        ref={scrollerRef}
      >
        <div className={styles.stage}>
          <div className={styles.canvas}>
            <img
              ref={imgRef}
              key={`${jobId}-${activePage}-${detail?.status ?? ""}`}
              src={pageImageUrl(jobId, activePage, detail?.status ?? "")}
              alt={`Page ${activePage}`}
              className={styles.image}
              style={imageStyle}
              onLoad={() => setImgLoaded(true)}
              draggable={false}
            />
            {overlays && imgLoaded && displaySize.w > 0 && displaySize.h > 0 && (
              <BboxOverlay
                sourceWidth={sourceWidth}
                sourceHeight={sourceHeight}
                displayWidth={displaySize.w}
                displayHeight={displaySize.h}
                page={activePage}
                fields={fields}
                lineItems={lineItems}
                activeFieldPath={activeFieldPath}
                hoverFieldPath={hoverFieldPath}
                ocrTokens={ocrTokens}
                onSelectField={setActiveField}
              />
            )}
          </div>
        </div>
      </div>
      <PageNav
        currentPage={activePage}
        pageCount={pageCount}
        onChange={handlePageChange}
      />
    </div>
  );
}

export default PdfPane;
