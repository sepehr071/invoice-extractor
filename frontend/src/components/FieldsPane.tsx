import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useReview } from "../state/useReview";
import type { FieldLink, Invoice, BankInfo } from "../api/types";
import FieldRow from "./FieldRow";
import LineItemsTable from "./LineItemsTable";
import { Section, type LinkSummary } from "./Section";
import styles from "./FieldsPane.module.css";

const CONFIDENCE_STORAGE_KEY = "invoice-ai.showConfidence";

type FieldDef = {
  path: string;
  label: string;
  type?: "text" | "number" | "date";
  read: (inv: Invoice) => string | number | null;
};

const COMPANY: FieldDef[] = [
  { path: "company_name", label: "Company Name", read: (i) => i.company_name },
  { path: "company_address", label: "Address", read: (i) => i.company_address },
  { path: "company_country", label: "Country", read: (i) => i.company_country },
  { path: "company_phone", label: "Phone", read: (i) => i.company_phone },
];

const INVOICE_INFO: FieldDef[] = [
  { path: "invoice_number", label: "Invoice No.", read: (i) => i.invoice_number },
  { path: "invoice_date", label: "Invoice Date", type: "date", read: (i) => i.invoice_date },
  { path: "currency", label: "Currency (ISO 4217)", read: (i) => i.currency },
  { path: "total_amount", label: "Total Amount", type: "number", read: (i) => i.total_amount },
  { path: "invoice_type", label: "Invoice Type", read: (i) => i.invoice_type },
];

const CONSIGNEE: FieldDef[] = [
  { path: "customer_name", label: "Customer", read: (i) => i.customer_name },
  { path: "consignee_name", label: "Consignee Name", read: (i) => i.consignee_name },
  { path: "consignee_address", label: "Consignee Address", read: (i) => i.consignee_address },
];

const EXPORTER_LEGACY: FieldDef[] = [
  { path: "exporter_name", label: "Exporter Name", read: (i) => i.exporter_name },
  { path: "exporter_address", label: "Exporter Address", read: (i) => i.exporter_address },
  { path: "exporter_tel", label: "Exporter Tel", read: (i) => i.exporter_tel },
  { path: "payable_to", label: "Payable To", read: (i) => i.payable_to },
  { path: "incoterm", label: "Incoterm", read: (i) => i.incoterm },
];

const TOTALS_LEGACY: FieldDef[] = [
  { path: "total_quantity_carton", label: "Total Cartons", type: "number", read: (i) => i.total_quantity_carton },
  { path: "total_quantity_box", label: "Total Boxes", type: "number", read: (i) => i.total_quantity_box },
  { path: "total_aggregate_amount", label: "Grand Total (legacy)", type: "number", read: (i) => i.total_aggregate_amount },
  { path: "deposit_received", label: "Deposit Received", type: "number", read: (i) => i.deposit_received },
  { path: "deposit_date", label: "Deposit Date", type: "date", read: (i) => i.deposit_date },
  { path: "remain_money", label: "Remaining", type: "number", read: (i) => i.remain_money },
];

const PAYMENT_LEGACY: FieldDef[] = [
  { path: "payment_terms", label: "Payment Terms", read: (i) => i.payment_terms },
  { path: "packing", label: "Packing", read: (i) => i.packing },
];

type BankScalarKey = Exclude<keyof BankInfo, "accounts" | "notices">;
const BANK_SCALARS: { path: string; label: string; key: BankScalarKey }[] = [
  { path: "bank.bank_name", label: "Bank Name", key: "bank_name" },
  { path: "bank.swift_code", label: "SWIFT Code", key: "swift_code" },
  { path: "bank.bank_country", label: "Bank Country", key: "bank_country" },
  { path: "bank.address", label: "Bank Address", key: "address" },
  { path: "bank.beneficiary", label: "Beneficiary", key: "beneficiary" },
  { path: "bank.account_number", label: "Account No. (legacy)", key: "account_number" },
];

function summarize(paths: string[], byPath: Record<string, FieldLink>): LinkSummary {
  let linked = 0;
  let total = 0;
  for (const p of paths) {
    const link = byPath[p];
    if (!link) continue;
    total += 1;
    if (link.bbox !== null) linked += 1;
  }
  return { linked, total };
}

function hasAnyValue(defs: FieldDef[], invoice: Invoice): boolean {
  return defs.some((d) => {
    const v = d.read(invoice);
    return v !== null && v !== undefined && String(v).trim() !== "";
  });
}

function isNonEmpty(v: unknown): boolean {
  return v !== null && v !== undefined && String(v).trim() !== "";
}

/** True when the extractor produced an Invoice object but every field is blank. */
function invoiceIsEmpty(invoice: Invoice): boolean {
  const scalarGroups = [COMPANY, INVOICE_INFO, CONSIGNEE, EXPORTER_LEGACY, TOTALS_LEGACY, PAYMENT_LEGACY];
  if (scalarGroups.some((g) => hasAnyValue(g, invoice))) return false;

  const bank = invoice.bank;
  if (bank) {
    const bankScalar = BANK_SCALARS.some((b) => isNonEmpty(bank[b.key]));
    if (bankScalar) return false;
    if ((bank.accounts ?? []).length > 0) return false;
    if ((bank.notices ?? []).length > 0) return false;
  }

  if ((invoice.line_items ?? []).length > 0) return false;
  if ((invoice.extra_notes ?? []).length > 0) return false;
  if ((invoice.remarks ?? []).length > 0) return false;

  return true;
}

type ChipDef = {
  id: string;
  label: string;
  count?: number;
};

export function FieldsPane() {
  const detail = useReview((s) => s.detail);

  // FieldsPane is only mounted for the invoice variant; narrow defensively.
  const result = detail?.result;
  const invoiceResult = result && result.kind === "invoice" ? result : null;
  const invoice = invoiceResult?.invoice;
  const fields = invoiceResult?.fields;
  const lineItemLinks = invoiceResult?.line_items;
  const status = detail?.status;
  const disabled = status === "approved";

  const [showConfidence, setShowConfidence] = useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    return window.localStorage.getItem(CONFIDENCE_STORAGE_KEY) === "1";
  });

  useEffect(() => {
    if (typeof window === "undefined") return;
    window.localStorage.setItem(CONFIDENCE_STORAGE_KEY, showConfidence ? "1" : "0");
  }, [showConfidence]);

  const fieldsByPath = useMemo(() => {
    const map: Record<string, FieldLink> = {};
    if (fields) {
      for (const f of fields) map[f.field_path] = f;
    }
    return map;
  }, [fields]);

  const paneRef = useRef<HTMLDivElement | null>(null);
  const sectionRefs = useRef<Map<string, HTMLElement>>(new Map());
  const [activeChip, setActiveChip] = useState<string>("company");

  const registerSection = useCallback((id: string) => {
    return (el: HTMLElement | null) => {
      if (el) sectionRefs.current.set(id, el);
      else sectionRefs.current.delete(id);
    };
  }, []);

  // Scroll-spy: pick the topmost section whose top is at or above the chip-bar bottom.
  useEffect(() => {
    const pane = paneRef.current;
    if (!pane) return;

    const onScroll = () => {
      const entries = Array.from(sectionRefs.current.entries());
      if (entries.length === 0) return;
      const paneTop = pane.getBoundingClientRect().top;
      // Section is "current" once its top crosses below this offset.
      const triggerY = paneTop + 80; // chip-bar height + slack
      let current = entries[0][0];
      for (const [id, el] of entries) {
        const top = el.getBoundingClientRect().top;
        if (top - triggerY <= 0) current = id;
        else break;
      }
      setActiveChip((prev) => (prev === current ? prev : current));
    };

    pane.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
    return () => pane.removeEventListener("scroll", onScroll);
  }, [invoice]);

  const scrollToSection = useCallback((id: string) => {
    const pane = paneRef.current;
    const el = sectionRefs.current.get(id);
    if (!pane || !el) return;
    // Ask the section to expand if collapsed.
    window.dispatchEvent(new CustomEvent("invoice-ai:expand-section", { detail: { id } }));
    // Defer scroll one frame so newly-expanded body is laid out.
    requestAnimationFrame(() => {
      const paneRect = pane.getBoundingClientRect();
      const elRect = el.getBoundingClientRect();
      const delta = elRect.top - paneRect.top - 64; // leave room for sticky chip bar
      pane.scrollTo({ top: pane.scrollTop + delta, behavior: "smooth" });
    });
    setActiveChip(id);
  }, []);

  if (!invoice) {
    return (
      <div className={styles.pane}>
        <div className={styles.placeholder}>Waiting for extraction results…</div>
      </div>
    );
  }

  const renderScalar = (def: FieldDef) => (
    <FieldRow
      key={def.path}
      fieldPath={def.path}
      label={def.label}
      type={def.type ?? "text"}
      value={def.read(invoice)}
      link={fieldsByPath[def.path]}
      disabled={disabled}
      showConfidence={showConfidence}
    />
  );

  const accounts = invoice.bank?.accounts ?? [];
  const notices = invoice.bank?.notices ?? [];
  const extraNotes = invoice.extra_notes ?? [];
  const remarks = invoice.remarks ?? [];
  const lineItemsCount = lineItemLinks?.length ?? invoice.line_items?.length ?? 0;

  const consigneeHasAny = hasAnyValue(CONSIGNEE, invoice);
  const extractionEmpty = invoiceIsEmpty(invoice);

  // Build bank field paths dynamically so summary covers accounts + notices too.
  const bankPaths: string[] = [
    ...BANK_SCALARS.map((b) => b.path),
    ...accounts.flatMap((_, i) => [
      `bank.accounts[${i}].currency`,
      `bank.accounts[${i}].account_number`,
      `bank.accounts[${i}].iban`,
      `bank.accounts[${i}].extra`,
    ]),
    ...notices.map((_, i) => `bank.notices[${i}]`),
  ];
  const extraNotePaths = extraNotes.map((_, i) => `extra_notes[${i}]`);

  const sumCompany = summarize(COMPANY.map((d) => d.path), fieldsByPath);
  const sumInvoice = summarize(INVOICE_INFO.map((d) => d.path), fieldsByPath);
  const sumConsignee = summarize(CONSIGNEE.map((d) => d.path), fieldsByPath);
  const sumBank = summarize(bankPaths, fieldsByPath);
  const sumNotes = summarize(extraNotePaths, fieldsByPath);

  const chips: ChipDef[] = [{ id: "company", label: "Company" }, { id: "invoice", label: "Invoice" }];
  if (consigneeHasAny) chips.push({ id: "buyer", label: "Buyer" });
  chips.push({ id: "items", label: "Items", count: lineItemsCount });
  chips.push({ id: "bank", label: "Bank" });
  chips.push({ id: "notes", label: "Notes", count: extraNotes.length });

  return (
    <div ref={paneRef} className={`${styles.pane} ${disabled ? styles.disabled : ""}`}>
      <div className={styles.chipBar}>
        <div className={styles.chipRow}>
          {chips.map((c) => (
            <button
              key={c.id}
              type="button"
              className={`${styles.chip} ${activeChip === c.id ? styles.chipActive : ""}`}
              onClick={() => scrollToSection(c.id)}
            >
              <span>{c.label}</span>
              {typeof c.count === "number" && (
                <span className={styles.chipCount}>{c.count}</span>
              )}
            </button>
          ))}
        </div>
        <button
          type="button"
          className={`${styles.toggleBtn} ${showConfidence ? styles.toggleBtnOn : ""}`}
          onClick={() => setShowConfidence((v) => !v)}
          title="Show or hide the rapidfuzz match score (0..100) on every field"
          aria-pressed={showConfidence}
        >
          <span className={styles.toggleDot} aria-hidden="true" />
          {showConfidence ? "Confidence: ON" : "Show confidence"}
        </button>
      </div>

      {disabled && (
        <div className={styles.approvedBanner}>
          This invoice has been approved. Fields are read-only.
        </div>
      )}

      {extractionEmpty && (
        <div className={styles.emptyExtraction} role="status">
          <span className={styles.emptyExtractionIcon} aria-hidden="true">!</span>
          <div>
            <strong>Extraction finished but found no fields.</strong>
            <p>
              The model returned no recognizable invoice data. Review the PDF on the left and
              add fields or line items manually.
            </p>
          </div>
        </div>
      )}

      <Section
        id="company"
        title="Company"
        summary={sumCompany}
        defaultExpanded
        sectionRef={registerSection("company")}
      >
        {COMPANY.map(renderScalar)}
      </Section>

      <Section
        id="invoice"
        title="Invoice Info"
        summary={sumInvoice}
        defaultExpanded
        sectionRef={registerSection("invoice")}
      >
        {INVOICE_INFO.map(renderScalar)}
      </Section>

      {consigneeHasAny && (
        <Section
          id="buyer"
          title="Buyer / Consignee"
          summary={sumConsignee}
          defaultExpanded={false}
          sectionRef={registerSection("buyer")}
        >
          {CONSIGNEE.map(renderScalar)}
        </Section>
      )}

      <Section
        id="items"
        title="Line Items"
        count={lineItemsCount}
        defaultExpanded
        sectionRef={registerSection("items")}
      >
        <LineItemsTable />
      </Section>

      <Section
        id="bank"
        title="Bank"
        summary={sumBank}
        defaultExpanded
        sectionRef={registerSection("bank")}
      >
        {BANK_SCALARS.map((def) => (
          <FieldRow
            key={def.path}
            fieldPath={def.path}
            label={def.label}
            value={invoice.bank?.[def.key] ?? null}
            link={fieldsByPath[def.path]}
            disabled={disabled}
            showConfidence={showConfidence}
          />
        ))}

        {accounts.length > 0 && (
          <div className={styles.subsection}>
            <h4 className={styles.subsectionTitle}>Accounts</h4>
            {accounts.map((acc, idx) => (
              <div key={`acct-${idx}`} className={styles.subgroup}>
                <FieldRow
                  fieldPath={`bank.accounts[${idx}].currency`}
                  label={`Acct ${idx + 1} · Currency`}
                  value={acc.currency}
                  link={fieldsByPath[`bank.accounts[${idx}].currency`]}
                  disabled={disabled}
                  showConfidence={showConfidence}
                />
                <FieldRow
                  fieldPath={`bank.accounts[${idx}].account_number`}
                  label={`Acct ${idx + 1} · Number`}
                  value={acc.account_number}
                  link={fieldsByPath[`bank.accounts[${idx}].account_number`]}
                  disabled={disabled}
                  showConfidence={showConfidence}
                />
                <FieldRow
                  fieldPath={`bank.accounts[${idx}].iban`}
                  label={`Acct ${idx + 1} · IBAN`}
                  value={acc.iban}
                  link={fieldsByPath[`bank.accounts[${idx}].iban`]}
                  disabled={disabled}
                  showConfidence={showConfidence}
                />
                {acc.extra && (
                  <FieldRow
                    fieldPath={`bank.accounts[${idx}].extra`}
                    label={`Acct ${idx + 1} · Extra`}
                    value={acc.extra}
                    link={fieldsByPath[`bank.accounts[${idx}].extra`]}
                    disabled={disabled}
                    showConfidence={showConfidence}
                  />
                )}
              </div>
            ))}
          </div>
        )}

        {notices.length > 0 && (
          <div className={styles.subsection}>
            <h4 className={styles.subsectionTitle}>Notices</h4>
            {notices.map((n, i) => (
              <FieldRow
                key={`bank.notices[${i}]`}
                fieldPath={`bank.notices[${i}]`}
                label={`Notice ${i + 1}`}
                value={n}
                link={fieldsByPath[`bank.notices[${i}]`]}
                disabled={disabled}
                showConfidence={showConfidence}
              />
            ))}
          </div>
        )}
      </Section>

      <Section
        id="notes"
        title="Extra Notes"
        count={extraNotes.length}
        summary={sumNotes}
        defaultExpanded={false}
        sectionRef={registerSection("notes")}
      >
        {extraNotes.length === 0 && (
          <div className={styles.remarkEmpty}>No extra notes.</div>
        )}
        {extraNotes.map((note, i) => (
          <FieldRow
            key={`extra_notes[${i}]`}
            fieldPath={`extra_notes[${i}]`}
            label={`Note ${i + 1}`}
            value={note}
            link={fieldsByPath[`extra_notes[${i}]`]}
            disabled={disabled}
            showConfidence={showConfidence}
          />
        ))}
      </Section>

      {hasAnyValue(EXPORTER_LEGACY, invoice) && (
        <Section
          id="exporter-legacy"
          title="Exporter (legacy)"
          defaultExpanded={false}
        >
          {EXPORTER_LEGACY.map(renderScalar)}
        </Section>
      )}
      {hasAnyValue(TOTALS_LEGACY, invoice) && (
        <Section
          id="totals-legacy"
          title="Totals (legacy)"
          defaultExpanded={false}
        >
          {TOTALS_LEGACY.map(renderScalar)}
        </Section>
      )}
      {hasAnyValue(PAYMENT_LEGACY, invoice) && (
        <Section
          id="payment-legacy"
          title="Payment (legacy)"
          defaultExpanded={false}
        >
          {PAYMENT_LEGACY.map(renderScalar)}
        </Section>
      )}

      {remarks.length > 0 && (
        <Section
          id="remarks-legacy"
          title="Remarks (legacy)"
          count={remarks.length}
          defaultExpanded={false}
        >
          {remarks.map((r, i) => (
            <FieldRow
              key={`remarks[${i}]`}
              fieldPath={`remarks[${i}]`}
              label={`Remark ${i + 1}`}
              value={r}
              link={fieldsByPath[`remarks[${i}]`]}
              disabled={disabled}
              showConfidence={showConfidence}
            />
          ))}
        </Section>
      )}
    </div>
  );
}

export default FieldsPane;
