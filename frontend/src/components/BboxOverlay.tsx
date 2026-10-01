import { useEffect, useMemo, useRef } from "react";
import type { FieldLink, LineItemLink, OcrToken, Polygon } from "../api/types";
import styles from "./BboxOverlay.module.css";

interface Props {
  sourceWidth: number;
  sourceHeight: number;
  displayWidth: number;
  displayHeight: number;
  page: number;
  fields: FieldLink[];
  lineItems: LineItemLink[];
  activeFieldPath: string | null;
  hoverFieldPath: string | null;
  ocrTokens: OcrToken[];
  onSelectField: (path: string | null) => void;
}

/** Convert a source-pixel polygon into an SVG `points` attribute string using scale ratios. */
function polygonToPoints(
  poly: Polygon,
  sx: number,
  sy: number
): string {
  return poly.map(([x, y]) => `${x * sx},${y * sy}`).join(" ");
}

/** Centroid of an arbitrary polygon in source-pixel space. */
function centroid(poly: Polygon): [number, number] {
  if (poly.length === 0) return [0, 0];
  let cx = 0;
  let cy = 0;
  for (const [x, y] of poly) {
    cx += x;
    cy += y;
  }
  return [cx / poly.length, cy / poly.length];
}

/** Ray-casting point-in-polygon. Coords assumed in same space (source pixels). */
function pointInPolygon(x: number, y: number, poly: Polygon): boolean {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const xi = poly[i][0];
    const yi = poly[i][1];
    const xj = poly[j][0];
    const yj = poly[j][1];
    const intersects =
      yi > y !== yj > y &&
      x < ((xj - xi) * (y - yi)) / (yj - yi + 1e-9) + xi;
    if (intersects) inside = !inside;
  }
  return inside;
}

function normalizeText(s: unknown): string {
  if (s == null) return "";
  return String(s).toLowerCase().replace(/\s+/g, " ").trim();
}

/** Length-aware partial-substring score (0..100), mirroring the backend linker. */
function fuzzyScore(needle: string, candidate: string): number {
  if (!needle || !candidate) return 0;
  const a = normalizeText(needle);
  const b = normalizeText(candidate);
  if (!a || !b) return 0;
  const short = a.length <= b.length ? a : b;
  const long = a.length <= b.length ? b : a;
  // Best alignment of short within long: substring presence -> 100, otherwise
  // count matching chars in best-aligned window.
  if (long.includes(short)) {
    const ratio = short.length / long.length;
    return 100 * ratio;
  }
  // Cheap char-set overlap fallback.
  let hits = 0;
  for (const ch of short) if (long.includes(ch)) hits++;
  return (hits / short.length) * 60;
}

function bestFieldForToken(
  tokenText: string,
  fields: FieldLink[]
): FieldLink | null {
  let best: { f: FieldLink; s: number } | null = null;
  for (const f of fields) {
    if (f.value == null) continue;
    const s = fuzzyScore(tokenText, String(f.value));
    if (s >= 60 && (!best || s > best.s)) best = { f, s };
  }
  return best ? best.f : null;
}

/**
 * BboxOverlay — SVG layer painted over the page image. Renders one polygon per
 * field on this page, plus dashed polygons for line-item rows. Captures clicks
 * either on those polygons directly OR on the empty area (reverse-lookup via
 * nearest OCR token, falling back to onSelectField(null)).
 */
export function BboxOverlay(props: Props) {
  const {
    sourceWidth,
    sourceHeight,
    displayWidth,
    displayHeight,
    page,
    fields,
    lineItems,
    activeFieldPath,
    hoverFieldPath,
    ocrTokens,
    onSelectField,
  } = props;

  const svgRef = useRef<SVGSVGElement | null>(null);

  const sx = sourceWidth > 0 ? displayWidth / sourceWidth : 1;
  const sy = sourceHeight > 0 ? displayHeight / sourceHeight : 1;

  const pageFields = useMemo(
    () =>
      fields.filter(
        (f) => f.page === page && f.bbox != null && f.bbox.length > 0
      ),
    [fields, page]
  );

  const pageLineItems = useMemo(
    () =>
      lineItems.filter(
        (li) => li.page === page && li.bbox != null && li.bbox.length > 0
      ),
    [lineItems, page]
  );

  // Auto-scroll the active field's polygon into view when it changes.
  useEffect(() => {
    if (!activeFieldPath || !svgRef.current) return;
    const el = svgRef.current.querySelector<SVGPolygonElement>(
      `[data-field-path="${CSS.escape(activeFieldPath)}"]`
    );
    if (el && typeof el.scrollIntoView === "function") {
      el.scrollIntoView({ behavior: "smooth", block: "center", inline: "center" });
    }
  }, [activeFieldPath, page]);

  // Click on empty SVG area → reverse-lookup via nearest OCR token.
  const handleBackgroundClick = (e: React.MouseEvent<SVGRectElement>) => {
    if (!svgRef.current || sx === 0 || sy === 0) {
      onSelectField(null);
      return;
    }
    const rect = svgRef.current.getBoundingClientRect();
    const dx = e.clientX - rect.left;
    const dy = e.clientY - rect.top;
    // Convert back to source coords.
    const px = dx / sx;
    const py = dy / sy;

    // First, see if click lands inside a field polygon (priority over OCR).
    for (const f of pageFields) {
      if (f.bbox && pointInPolygon(px, py, f.bbox)) {
        onSelectField(f.field_path);
        return;
      }
    }
    // Then check line items.
    for (const li of pageLineItems) {
      if (li.bbox && pointInPolygon(px, py, li.bbox)) {
        onSelectField(`line_item:${li.row_index}`);
        return;
      }
    }

    // Hit-test OCR tokens directly (precise — no centroid distance).
    for (const tok of ocrTokens) {
      if (pointInPolygon(px, py, tok.bbox)) {
        const f = bestFieldForToken(tok.text, fields);
        if (f) {
          onSelectField(f.field_path);
          return;
        }
        // Token clicked but no field matches — fall through to null.
        break;
      }
    }
    onSelectField(null);
  };

  const handleTokenClick = (tok: OcrToken) => (e: React.MouseEvent) => {
    e.stopPropagation();
    const f = bestFieldForToken(tok.text, fields);
    if (f) onSelectField(f.field_path);
    else onSelectField(null);
  };

  const handleFieldClick = (path: string) => (e: React.MouseEvent) => {
    e.stopPropagation();
    onSelectField(path);
  };

  if (sourceWidth <= 0 || sourceHeight <= 0) return null;

  return (
    <svg
      ref={svgRef}
      className={styles.overlay}
      width={displayWidth}
      height={displayHeight}
      viewBox={`0 0 ${displayWidth} ${displayHeight}`}
      preserveAspectRatio="none"
    >
      {/* Transparent backdrop captures clicks outside any polygon */}
      <rect
        x={0}
        y={0}
        width={displayWidth}
        height={displayHeight}
        fill="transparent"
        onClick={handleBackgroundClick}
      />

      {/* Invisible per-token polygons — click anywhere on the page text and
          we'll match it to the most-likely field. Sit underneath line-items
          and fields so the explicit polygons keep priority. */}
      {ocrTokens.map((tok, i) => (
        <polygon
          key={`tok-${i}`}
          className={`${styles.poly} ${styles.token}`}
          points={polygonToPoints(tok.bbox, sx, sy)}
          onClick={handleTokenClick(tok)}
          pointerEvents="all"
        >
          <title>{tok.text}</title>
        </polygon>
      ))}

      {/* Line items first (dashed, beneath fields) */}
      {pageLineItems.map((li) => {
        if (!li.bbox) return null;
        const key = `li-${li.row_index}`;
        const path = `line_item:${li.row_index}`;
        const isActive = activeFieldPath === path;
        const isHover = hoverFieldPath === path;
        const cls = [
          styles.poly,
          styles.lineItem,
          isHover ? styles.hover : "",
          isActive ? styles.active : "",
        ]
          .filter(Boolean)
          .join(" ");
        return (
          <polygon
            key={key}
            data-field-path={path}
            className={cls}
            points={polygonToPoints(li.bbox, sx, sy)}
            onClick={handleFieldClick(path)}
            pointerEvents="all"
          />
        );
      })}

      {/* Header fields on top */}
      {pageFields.map((f) => {
        if (!f.bbox) return null;
        const isActive = activeFieldPath === f.field_path;
        const isHover = hoverFieldPath === f.field_path;
        const cls = [
          styles.poly,
          styles.field,
          isHover ? styles.hover : "",
          isActive ? styles.active : "",
        ]
          .filter(Boolean)
          .join(" ");
        return (
          <polygon
            key={f.field_path}
            data-field-path={f.field_path}
            className={cls}
            points={polygonToPoints(f.bbox, sx, sy)}
            onClick={handleFieldClick(f.field_path)}
            pointerEvents="all"
          />
        );
      })}
    </svg>
  );
}

export default BboxOverlay;
