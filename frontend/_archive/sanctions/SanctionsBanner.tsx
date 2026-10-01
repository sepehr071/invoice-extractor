import { useState } from "react";
import { getJob, rescreenJob } from "../api/client";
import { useReview } from "../state/useReview";
import type { Invoice, SanctionsRole, SanctionsVerdict } from "../api/types";
import { SanctionsPanelBody, screenedPartyFromInvoice } from "./SanctionsPanel";
import panelStyles from "./SanctionsPanel.module.css";
import styles from "./SanctionsBanner.module.css";

interface Props {
  verdict: SanctionsVerdict | null | undefined;
  invoice?: Invoice | null;
}

type RollupSeverity = "green" | "yellow" | "red" | "not_extracted";

const ROLLUP_ORDER: SanctionsRole[] = [
  "seller",
  "bank",
  "consignee",
  "customer",
  "beneficiary",
  "payable_to",
];

const ROLLUP_ROLE_LABEL: Record<SanctionsRole, string> = {
  seller: "Seller",
  bank: "Bank",
  consignee: "Consignee",
  customer: "Customer",
  beneficiary: "Beneficiary",
  payable_to: "Payable-to",
};

/** Per-role severity from match list. Mirrors SanctionsBadge.severityForRole
 *  but adds a "not_extracted" tri-state for rows whose invoice field is empty. */
function rollupSeverity(
  verdict: SanctionsVerdict,
  role: SanctionsRole,
  hasName: boolean
): RollupSeverity {
  if (!hasName) return "not_extracted";
  const roleMatches = (verdict.matches ?? []).filter((m) => m.role === role);
  if (roleMatches.length === 0) return "green";

  if (
    roleMatches.some(
      (m) => m.llm_verdict === "confirmed" || m.llm_verdict === "skipped"
    )
  )
    return "red";
  if (
    roleMatches.some(
      (m) => m.llm_verdict === "unclear" || m.llm_verdict === "error"
    )
  )
    return "yellow";
  if (roleMatches.every((m) => m.llm_verdict === "rejected")) return "green";
  // Pending / cached pre-LLM — fall back to rapidfuzz score for visibility.
  if (roleMatches.some((m) => m.score >= 95)) return "red";
  return "yellow";
}

function rollupIcon(sev: RollupSeverity): string {
  switch (sev) {
    case "green":
      return "✓";
    case "yellow":
      return "!";
    case "red":
      return "⚠";
    default:
      return "—";
  }
}

function rollupLabel(sev: RollupSeverity, matchCount: number): string {
  switch (sev) {
    case "green":
      return matchCount > 0 ? "clean (LLM cleared)" : "clean";
    case "yellow":
      return `${matchCount} review`;
    case "red":
      return `${matchCount} match${matchCount === 1 ? "" : "es"}`;
    default:
      return "not extracted";
  }
}

function rollupToneClass(sev: RollupSeverity): string {
  switch (sev) {
    case "red":
      return panelStyles.rollupRed;
    case "yellow":
      return panelStyles.rollupYellow;
    case "green":
      return panelStyles.rollupGreen;
    default:
      return panelStyles.rollupNeutral;
  }
}

/** Status -> {icon, bar tint class, headline}. Drives the bar visuals.
 *  Kept inline to avoid scattering verdict-string logic across components. */
function statusFace(verdict: SanctionsVerdict): {
  icon: string;
  barClass: string;
  headline: string;
} {
  const matches = verdict.matches ?? [];
  const confirmed = matches.filter(
    (m) => m.llm_verdict === "confirmed" || m.llm_verdict === "skipped"
  ).length;
  const unclear = matches.filter(
    (m) => m.llm_verdict === "unclear" || m.llm_verdict === "error"
  ).length;
  const rejected = matches.filter((m) => m.llm_verdict === "rejected").length;

  const bits: string[] = [];
  if (confirmed > 0) bits.push(`${confirmed} confirmed`);
  if (unclear > 0) bits.push(`${unclear} unclear`);
  if (rejected > 0) bits.push(`${rejected} LLM-rejected`);

  switch (verdict.status) {
    case "red":
      return {
        icon: "!",
        barClass: styles.barRed,
        headline: `Sanctions HIT${bits.length ? " — " + bits.join(" · ") : ""}`,
      };
    case "yellow":
      return {
        icon: "⚠",
        barClass: styles.barYellow,
        headline: `Sanctions review needed${bits.length ? " — " + bits.join(" · ") : ""}`,
      };
    case "green":
      return {
        icon: "✓",
        barClass: styles.barGreen,
        headline:
          bits.length > 0
            ? `Sanctions cleared by LLM — ${bits.join(" · ")}`
            : "Sanctions cleared — 0 candidates above threshold",
      };
    case "not_checked":
      return {
        icon: "—",
        barClass: styles.barNeutral,
        headline: "Sanctions screening — not checked",
      };
    case "error":
    default:
      return {
        icon: "×",
        barClass: styles.barError,
        headline: `Sanctions screening failed${verdict.error ? ` — ${verdict.error}` : ""}`,
      };
  }
}

export function SanctionsBanner({ verdict, invoice }: Props): JSX.Element | null {
  const [open, setOpen] = useState(false);
  const [rescreening, setRescreening] = useState(false);
  const [rescreenError, setRescreenError] = useState<string | null>(null);
  const [drilldownRole, setDrilldownRole] = useState<SanctionsRole | "all">("all");
  const jobId = useReview((s) => s.jobId);
  const setDetail = useReview((s) => s.setDetail);

  if (!verdict) return null;

  const { icon, barClass, headline } = statusFace(verdict);
  const allowRescreen = verdict.status !== "not_checked";

  const onRescreen = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!jobId || rescreening) return;
    setRescreening(true);
    setRescreenError(null);
    try {
      await rescreenJob(jobId);
      const fresh = await getJob(jobId);
      setDetail(fresh);
    } catch (err) {
      setRescreenError((err as Error).message);
    } finally {
      setRescreening(false);
    }
  };

  return (
    <div className={styles.wrapper}>
      <div className={`${styles.bar} ${barClass}`}>
        <span className={styles.icon} aria-hidden="true">
          {icon}
        </span>
        <span className={styles.summary}>{headline}</span>
        <div className={styles.actions}>
          {allowRescreen && (
            <button
              type="button"
              className={styles.rescreenBtn}
              onClick={onRescreen}
              disabled={rescreening}
              title="Re-run sanctions screening on this job against the current downloaded lists"
            >
              {rescreening ? "Rescreening…" : "↻ Rescreen"}
            </button>
          )}
          <button
            type="button"
            className={styles.whyBtn}
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            aria-controls="sanctions-banner-body"
          >
            Why
            <span className={`${styles.chev} ${open ? styles.chevOpen : ""}`} aria-hidden="true">
              ▾
            </span>
          </button>
        </div>
      </div>

      {rescreenError && <div className={styles.rescreenError}>{rescreenError}</div>}

      <div
        id="sanctions-banner-body"
        className={`${styles.body} ${open ? styles.bodyOpen : ""}`}
        aria-hidden={!open}
      >
        {open && (
          <>
            <ScreenedPartiesRollup
              verdict={verdict}
              invoice={invoice}
              onPickRole={(r) => setDrilldownRole(r)}
            />
            <SanctionsPanelBody
              verdict={verdict}
              invoice={invoice}
              initialRoleFilter={drilldownRole}
            />
          </>
        )}
      </div>
    </div>
  );
}

function ScreenedPartiesRollup({
  verdict,
  invoice,
  onPickRole,
}: {
  verdict: SanctionsVerdict;
  invoice: Invoice | null | undefined;
  onPickRole: (role: SanctionsRole | "all") => void;
}) {
  const rows = ROLLUP_ORDER.map((role) => {
    const { name } = screenedPartyFromInvoice(invoice, role);
    const matchCount = (verdict.matches ?? []).filter((m) => m.role === role).length;
    const sev = rollupSeverity(verdict, role, Boolean(name && name.trim()));
    return { role, name, matchCount, sev };
  });

  // Hide roles that are neither screened nor present in matches — keeps the
  // grid tight for invoices that only fill 2-3 of the 6 fields.
  const visible = rows.filter((r) => (r.name && r.name.trim()) || r.matchCount > 0);
  if (visible.length === 0) return null;

  return (
    <div className={panelStyles.rollup}>
      <span className={panelStyles.rollupLabel}>Screened parties</span>
      <div className={panelStyles.rollupGrid}>
        {visible.map(({ role, name, matchCount, sev }) => {
          const clickable = matchCount > 0;
          const toneCls = rollupToneClass(sev);
          const onClick = () => {
            if (clickable) onPickRole(role);
          };
          return (
            <div
              key={role}
              className={`${panelStyles.rollupRow} ${clickable ? panelStyles.rollupRowClickable : ""}`}
              onClick={onClick}
              role={clickable ? "button" : undefined}
              tabIndex={clickable ? 0 : undefined}
              onKeyDown={(e) => {
                if (clickable && (e.key === "Enter" || e.key === " ")) {
                  e.preventDefault();
                  onClick();
                }
              }}
              title={
                clickable
                  ? `Filter match list to ${ROLLUP_ROLE_LABEL[role]}`
                  : undefined
              }
            >
              <span className={panelStyles.rollupRole}>
                {ROLLUP_ROLE_LABEL[role]}
              </span>
              <span className={`${panelStyles.rollupStatus} ${toneCls}`}>
                <span className={panelStyles.rollupStatusIcon}>
                  {rollupIcon(sev)}
                </span>
                {rollupLabel(sev, matchCount)}
              </span>
              <span
                className={`${panelStyles.rollupName} ${
                  !name || !name.trim() ? panelStyles.rollupNameMissing : ""
                }`}
              >
                {name && name.trim() ? name : "—"}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default SanctionsBanner;
