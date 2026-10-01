import { useEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import type { Rect } from "../api/types";
import styles from "./RoiCanvas.module.css";

interface Props {
  /** Original page width in source-image pixels. */
  sourceWidth: number;
  /** Original page height in source-image pixels. */
  sourceHeight: number;
  /** Rendered display width of the underlying page image. */
  displayWidth: number;
  /** Rendered display height of the underlying page image. */
  displayHeight: number;
  /** Current rects for THIS page, in source-pixel coords. */
  rects: Rect[];
  /** Notify parent of any rects change (add / move / delete). */
  onChange: (next: Rect[]) => void;
  /** Disable all interaction (e.g. after Start has been clicked). */
  disabled?: boolean;
  /** Controlled selection index (null = nothing selected). Optional. */
  selectedIndex?: number | null;
  /** Notify parent when the selected rect index changes. */
  onSelectedIndexChange?: (idx: number | null) => void;
}

/** Smallest rect (in source-pixel units) we accept as a deliberate draw. */
const MIN_RECT_PX = 6;

function normaliseRect(a: { x: number; y: number }, b: { x: number; y: number }): Rect {
  const x = Math.min(a.x, b.x);
  const y = Math.min(a.y, b.y);
  const w = Math.abs(a.x - b.x);
  const h = Math.abs(a.y - b.y);
  return { x, y, w, h };
}

function clampRect(r: Rect, maxW: number, maxH: number): Rect {
  const x = Math.max(0, Math.min(maxW, r.x));
  const y = Math.max(0, Math.min(maxH, r.y));
  const w = Math.max(0, Math.min(maxW - x, r.w));
  const h = Math.max(0, Math.min(maxH - y, r.h));
  return { x, y, w, h };
}

export function RoiCanvas({
  sourceWidth,
  sourceHeight,
  displayWidth,
  displayHeight,
  rects,
  onChange,
  disabled = false,
  selectedIndex,
  onSelectedIndexChange,
}: Props) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [drag, setDrag] = useState<{ start: { x: number; y: number }; current: { x: number; y: number } } | null>(null);
  const [internalSelected, setInternalSelected] = useState<number | null>(null);

  // Selection is controlled when the parent passes `selectedIndex`; otherwise
  // it falls back to internal state.
  const controlled = selectedIndex !== undefined;
  const selectedIdx = controlled ? selectedIndex! : internalSelected;
  const setSelectedIdx = (idx: number | null) => {
    if (!controlled) setInternalSelected(idx);
    onSelectedIndexChange?.(idx);
  };

  // Display ↔ source-pixel scaling. SVG runs in source-pixel space via viewBox,
  // so all rect coords stay in source units; the SVG element itself stretches
  // to displayWidth/displayHeight to match the image element.
  const scaleX = sourceWidth > 0 ? displayWidth / sourceWidth : 1;
  const scaleY = sourceHeight > 0 ? displayHeight / sourceHeight : 1;

  const pointerToSource = (e: ReactPointerEvent<SVGSVGElement>): { x: number; y: number } => {
    const svg = svgRef.current;
    if (!svg) return { x: 0, y: 0 };
    const rect = svg.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;
    return {
      x: Math.round(px / (scaleX || 1)),
      y: Math.round(py / (scaleY || 1)),
    };
  };

  const onPointerDown = (e: ReactPointerEvent<SVGSVGElement>) => {
    if (disabled) return;
    if (e.button !== 0) return;
    // Only start a draw on background pointer-down (not on an existing rect).
    if ((e.target as SVGElement).dataset.roiHandle === "rect") return;

    e.currentTarget.setPointerCapture(e.pointerId);
    const p = pointerToSource(e);
    setDrag({ start: p, current: p });
    setSelectedIdx(null);
  };

  const onPointerMove = (e: ReactPointerEvent<SVGSVGElement>) => {
    if (!drag) return;
    const p = pointerToSource(e);
    setDrag({ start: drag.start, current: p });
  };

  const onPointerUp = (e: ReactPointerEvent<SVGSVGElement>) => {
    if (!drag) return;
    e.currentTarget.releasePointerCapture(e.pointerId);
    const r = clampRect(normaliseRect(drag.start, drag.current), sourceWidth, sourceHeight);
    setDrag(null);
    if (r.w >= MIN_RECT_PX && r.h >= MIN_RECT_PX) {
      onChange([...rects, r]);
      setSelectedIdx(rects.length);
    }
  };

  const onDeleteSelected = () => {
    if (disabled || selectedIdx === null) return;
    const next = rects.filter((_, i) => i !== selectedIdx);
    setSelectedIdx(null);
    onChange(next);
  };

  // Delete / Backspace removes the selected rect when the canvas has focus
  // (i.e. user clicked into it).
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (selectedIdx === null) return;
      if (ev.key === "Delete" || ev.key === "Backspace") {
        ev.preventDefault();
        onDeleteSelected();
      } else if (ev.key === "Escape") {
        setSelectedIdx(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedIdx, rects, disabled]);

  if (sourceWidth <= 0 || sourceHeight <= 0) return null;

  const previewRect = drag
    ? clampRect(normaliseRect(drag.start, drag.current), sourceWidth, sourceHeight)
    : null;

  return (
    <svg
      ref={svgRef}
      className={`${styles.canvas} ${disabled ? styles.disabled : ""}`}
      width={displayWidth}
      height={displayHeight}
      viewBox={`0 0 ${sourceWidth} ${sourceHeight}`}
      preserveAspectRatio="none"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={() => setDrag(null)}
    >
      {/* Dim overlay outside the rect union helps the user see what gets sent
          to OCR. We render the dim layer everywhere and "punch through" each
          rect with a clip-path made of the rects. */}
      <defs>
        <mask id="roi-mask">
          <rect x={0} y={0} width={sourceWidth} height={sourceHeight} fill="white" />
          {rects.map((r, i) => (
            <rect key={`mask-${i}`} x={r.x} y={r.y} width={r.w} height={r.h} fill="black" />
          ))}
          {previewRect && previewRect.w > 0 && previewRect.h > 0 && (
            <rect
              x={previewRect.x}
              y={previewRect.y}
              width={previewRect.w}
              height={previewRect.h}
              fill="black"
            />
          )}
        </mask>
      </defs>

      {rects.length > 0 || previewRect ? (
        <rect
          x={0}
          y={0}
          width={sourceWidth}
          height={sourceHeight}
          className={styles.dim}
          mask="url(#roi-mask)"
        />
      ) : null}

      {rects.map((r, i) => (
        <g key={i}>
          <rect
            data-roi-handle="rect"
            x={r.x}
            y={r.y}
            width={r.w}
            height={r.h}
            className={`${styles.rect} ${selectedIdx === i ? styles.rectSelected : ""}`}
            onPointerDown={(e) => {
              if (disabled) return;
              e.stopPropagation();
              setSelectedIdx(i);
            }}
          />
          {selectedIdx === i && !disabled && (
            // Anchor the delete button to the rect's TOP edge (y = r.y) instead
            // of above it (y = r.y - 24), so it never spills off the top of the
            // page when the rect sits near y=0. The button hugs the inner
            // top-right corner. Box is sized in source-pixel (viewBox) units.
            <foreignObject
              x={r.x + r.w}
              y={r.y}
              width={1}
              height={1}
              style={{ overflow: "visible" }}
            >
              <button
                type="button"
                className={styles.deleteBtn}
                onClick={(e) => {
                  e.stopPropagation();
                  onDeleteSelected();
                }}
                title="Delete region (Del)"
              >
                ×
              </button>
            </foreignObject>
          )}
        </g>
      ))}

      {previewRect && previewRect.w > 0 && previewRect.h > 0 && (
        <rect
          x={previewRect.x}
          y={previewRect.y}
          width={previewRect.w}
          height={previewRect.h}
          className={styles.preview}
        />
      )}
    </svg>
  );
}

export default RoiCanvas;
