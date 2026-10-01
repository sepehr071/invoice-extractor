import { useEffect, useRef, useState } from "react";
import type { ChangeEvent } from "react";
import { useReview } from "../state/useReview";
import type { LineItem, LineItemLink } from "../api/types";
import { recordLastSave } from "./lastSave";
import styles from "./LineItemsTable.module.css";

type Col = {
  key: keyof LineItem;
  label: string;
  type: "text" | "number";
  width?: string;
  /** Legacy / extra-detail columns hidden behind the "Show all columns" toggle. */
  legacy?: boolean;
};

const COLS: Col[] = [
  { key: "no", label: "#", type: "number", width: "44px" },
  // Explicit width: under table-layout:fixed an auto column collapses to 0
  // once the other fixed columns exceed the pane width.
  { key: "product_name", label: "Product", type: "text", width: "240px" },
  { key: "category", label: "Category", type: "text", width: "150px" },
  { key: "count_type", label: "Count Type", type: "text", width: "90px" },
  { key: "unit", label: "Unit", type: "text", width: "70px" },
  { key: "quantity", label: "Qty", type: "number", width: "80px" },
  { key: "unit_price", label: "Unit Price", type: "number", width: "100px" },
  { key: "line_currency", label: "Cur.", type: "text", width: "70px" },
  { key: "line_total", label: "Line Total", type: "number", width: "110px" },
  // legacy / extra detail — hidden unless "Show all columns" is on
  { key: "size", label: "Size", type: "text", width: "90px", legacy: true },
  { key: "colour", label: "Colour", type: "text", width: "90px", legacy: true },
  { key: "box_per_carton", label: "Box/Carton", type: "text", width: "110px", legacy: true },
  { key: "piece_per_box", label: "Piece/Box", type: "text", width: "110px", legacy: true },
  { key: "weight_per_box", label: "Wt/Box", type: "number", width: "80px", legacy: true },
  { key: "quantity_carton", label: "Qty Cart", type: "number", width: "80px", legacy: true },
  { key: "quantity_box", label: "Qty Box", type: "number", width: "80px", legacy: true },
  { key: "fob_unit_price_usd", label: "Unit USD", type: "number", width: "90px", legacy: true },
  { key: "aggregate_amount", label: "Agg. Amt", type: "number", width: "100px", legacy: true },
];

/** Empty when every value in the item is null/blank. */
function lineItemIsEmpty(item: LineItem): boolean {
  return Object.values(item).every(
    (v) => v === null || v === undefined || String(v).trim() === ""
  );
}

const PRODUCT_KEY: keyof LineItem = "product_name";
const NO_KEY: keyof LineItem = "no";

/** Sticky class for the frozen leading columns (# + Product), or "" otherwise. */
function stickyClass(key: keyof LineItem, kind: "cell" | "header"): string {
  if (key === NO_KEY) return kind === "header" ? styles.stickyNoHeader : styles.stickyNoCell;
  if (key === PRODUCT_KEY) return kind === "header" ? styles.stickyHeader : styles.stickyCell;
  return "";
}

const EMPTY_ITEM: LineItem = {
  no: null,
  product_name: null,
  category: null,
  count_type: null,
  unit: null,
  quantity: null,
  unit_price: null,
  line_currency: null,
  line_total: null,
  size: null,
  colour: null,
  weight_per_box: null,
  box_per_carton: null,
  piece_per_box: null,
  quantity_carton: null,
  quantity_box: null,
  fob_unit_price_usd: null,
  aggregate_amount: null,
};

function parseCell(raw: string, type: "text" | "number"): string | number | null {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  if (type === "number") {
    const n = Number(trimmed);
    return Number.isFinite(n) ? n : null;
  }
  return trimmed;
}

function cellDisplay(v: string | number | null): string {
  if (v === null || v === undefined) return "";
  return String(v);
}

interface RowProps {
  link: LineItemLink;
  disabled: boolean;
  cols: Col[];
}

function ExistingRow({ link, disabled, cols }: RowProps) {
  const setActiveField = useReview((s) => s.setActiveField);
  const setHoverField = useReview((s) => s.setHoverField);
  const activeFieldPath = useReview((s) => s.activeFieldPath);
  const patchLineItem = useReview((s) => s.patchLineItem);
  const deleteLineItem = useReview((s) => s.deleteLineItem);

  const [confirmingDelete, setConfirmingDelete] = useState(false);

  const fieldKey = `line_item:${link.row_index}`;
  const active = activeFieldPath === fieldKey;

  const commitCell = (col: Col, raw: string) => {
    const parsed = parseCell(raw, col.type);
    const current = link.data[col.key] ?? null;
    const same =
      (parsed === null && current === null) ||
      (parsed !== null && current !== null && String(parsed) === String(current));
    if (same) return;
    const updated: LineItem = { ...link.data, [col.key]: parsed } as LineItem;
    const run = () => void patchLineItem(link.row_index, { data: updated });
    recordLastSave(run);
    run();
  };

  const confirmDeleteNow = () => {
    setConfirmingDelete(false);
    void deleteLineItem(link.row_index);
  };

  // Name the product (not the row index) in the confirm.
  const productLabel =
    (link.data.product_name && String(link.data.product_name).trim()) ||
    (link.data.no != null ? `row #${link.data.no}` : `row ${link.row_index + 1}`);

  return (
    <tr
      className={`${styles.row} ${active ? styles.activeRow : ""} ${link.edited ? styles.editedRow : ""}`}
      onMouseEnter={() => setHoverField(fieldKey)}
      onMouseLeave={() => setHoverField(null)}
      onClick={() => setActiveField(fieldKey)}
    >
      {cols.map((col) => (
        <td key={col.key} className={`${styles.cell} ${stickyClass(col.key, "cell")}`}>
          <CellInput
            // Key-based remount keeps the input synced to props when not
            // focused, without the render-body setState anti-pattern. The key
            // changes only when the committed value changes.
            key={`${link.row_index}:${col.key}:${cellDisplay(
              link.data[col.key] as string | number | null
            )}`}
            initial={cellDisplay(link.data[col.key] as string | number | null)}
            type={col.type}
            disabled={disabled}
            onCommit={(raw) => commitCell(col, raw)}
          />
        </td>
      ))}
      <td className={styles.actionCell}>
        {confirmingDelete ? (
          <span className={styles.confirmDelete} onClick={(e) => e.stopPropagation()}>
            <span className={styles.confirmDeleteText} title={`Delete “${productLabel}”?`}>
              Delete “{productLabel}”?
            </span>
            <button
              type="button"
              className={styles.confirmYes}
              onClick={(e) => {
                e.stopPropagation();
                confirmDeleteNow();
              }}
            >
              Delete
            </button>
            <button
              type="button"
              className={styles.confirmNo}
              onClick={(e) => {
                e.stopPropagation();
                setConfirmingDelete(false);
              }}
            >
              Cancel
            </button>
          </span>
        ) : (
          <button
            type="button"
            className={styles.deleteBtn}
            onClick={(e) => {
              e.stopPropagation();
              if (!disabled) setConfirmingDelete(true);
            }}
            disabled={disabled}
            title="Delete row"
          >
            ×
          </button>
        )}
      </td>
    </tr>
  );
}

interface CellInputProps {
  initial: string;
  type: "text" | "number";
  disabled: boolean;
  onCommit: (raw: string) => void;
  autoFocus?: boolean;
}

function CellInput({ initial, type, disabled, onCommit, autoFocus }: CellInputProps) {
  // No props→state resync here: the parent remounts this component (via a `key`
  // derived from the committed value) whenever `initial` changes, so this local
  // state is always fresh on mount. Editing a blurred cell is never clobbered.
  const [val, setVal] = useState(initial);

  return (
    <input
      className={styles.cellInput}
      type={type === "number" ? "number" : "text"}
      dir="auto"
      value={val}
      disabled={disabled}
      autoFocus={autoFocus}
      onChange={(e: ChangeEvent<HTMLInputElement>) => setVal(e.target.value)}
      onBlur={() => onCommit(val)}
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => {
        if (e.key === "Enter") e.currentTarget.blur();
        else if (e.key === "Escape") {
          setVal(initial);
          e.currentTarget.blur();
        }
      }}
      step={type === "number" ? "any" : undefined}
    />
  );
}

interface NewRowProps {
  disabled: boolean;
  cols: Col[];
  onCancel: () => void;
  onSave: (item: LineItem) => void;
}

function NewRow({ disabled, cols, onCancel, onSave }: NewRowProps) {
  const [draft, setDraft] = useState<LineItem>(EMPTY_ITEM);
  const firstInputRef = useRef<HTMLInputElement | null>(null);

  // Focus the first cell of the new row and scroll it into view.
  useEffect(() => {
    const el = firstInputRef.current;
    if (!el) return;
    el.focus();
    el.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "nearest" });
  }, []);

  const updateCol = (col: Col, raw: string) => {
    setDraft((d) => ({ ...d, [col.key]: parseCell(raw, col.type) }) as LineItem);
  };

  // Block Save when the whole row is empty.
  const empty = lineItemIsEmpty(draft);

  return (
    <tr className={`${styles.row} ${styles.newRow}`}>
      {cols.map((col, i) => (
        <td key={col.key} className={`${styles.cell} ${stickyClass(col.key, "cell")}`}>
          <input
            ref={i === 0 ? firstInputRef : undefined}
            className={styles.cellInput}
            type={col.type === "number" ? "number" : "text"}
            disabled={disabled}
            defaultValue=""
            onChange={(e) => updateCol(col, e.target.value)}
            onClick={(e) => e.stopPropagation()}
            step={col.type === "number" ? "any" : undefined}
          />
        </td>
      ))}
      <td className={styles.actionCell}>
        <button
          type="button"
          className={styles.saveBtn}
          onClick={() => onSave(draft)}
          disabled={disabled || empty}
          title={empty ? "Fill at least one cell to save" : "Save row"}
        >
          ✓
        </button>
        <button
          type="button"
          className={styles.cancelBtn}
          onClick={onCancel}
          disabled={disabled}
          title="Cancel"
        >
          ×
        </button>
      </td>
    </tr>
  );
}

const LEGACY_COUNT = COLS.filter((c) => c.legacy).length;

export function LineItemsTable() {
  const lineItems = useReview((s) =>
    s.detail?.result?.kind === "invoice" ? s.detail.result.line_items : []
  );
  const status = useReview((s) => s.detail?.status);
  const addLineItem = useReview((s) => s.addLineItem);

  const disabled = status === "approved";
  const [adding, setAdding] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Hide legacy columns by default so Product/Qty/Total stay visible.
  const cols = showAll ? COLS : COLS.filter((c) => !c.legacy);

  const sorted = [...lineItems].sort((a, b) => a.row_index - b.row_index);

  const onAddSave = (item: LineItem) => {
    void addLineItem(item);
    setAdding(false);
    // Reveal the newly-appended row.
    requestAnimationFrame(() => {
      const el = scrollRef.current;
      if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    });
  };

  return (
    <div className={styles.wrap}>
      <div className={styles.tableToolbar}>
        <button
          type="button"
          className={`${styles.colToggle} ${showAll ? styles.colToggleOn : ""}`}
          onClick={() => setShowAll((v) => !v)}
          aria-pressed={showAll}
          title={`${showAll ? "Hide" : "Show"} ${LEGACY_COUNT} legacy / extra columns`}
        >
          {showAll ? "Hide extra columns" : `Show all columns (+${LEGACY_COUNT})`}
        </button>
      </div>
      <div className={styles.tableScroll} ref={scrollRef}>
        <table className={styles.table}>
          <colgroup>
            {cols.map((c) => (
              <col key={c.key} style={c.width ? { width: c.width } : undefined} />
            ))}
            <col style={{ width: "60px" }} />
          </colgroup>
          <thead>
            <tr>
              {cols.map((c) => (
                <th key={c.key} className={`${styles.headerCell} ${stickyClass(c.key, "header")}`}>
                  {c.label}
                </th>
              ))}
              <th className={styles.headerCell}></th>
            </tr>
          </thead>
          <tbody>
            {sorted.length === 0 && !adding && (
              <tr>
                <td colSpan={cols.length + 1} className={styles.empty}>
                  No line items.
                </td>
              </tr>
            )}
            {sorted.map((link) => (
              <ExistingRow key={link.row_index} link={link} disabled={disabled} cols={cols} />
            ))}
            {adding && (
              <NewRow
                disabled={disabled}
                cols={cols}
                onCancel={() => setAdding(false)}
                onSave={onAddSave}
              />
            )}
          </tbody>
        </table>
      </div>
      {!disabled && !adding && (
        <button type="button" className={styles.addBtn} onClick={() => setAdding(true)}>
          + Add row
        </button>
      )}
    </div>
  );
}

export default LineItemsTable;
