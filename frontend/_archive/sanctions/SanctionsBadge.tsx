import type { SanctionsVerdict, SanctionsRole } from "../api/types";
import styles from "./SanctionsBadge.module.css";

interface Props {
  verdict: SanctionsVerdict | null | undefined;
  /**
   * When set, the badge reflects only matches whose `role` equals this filter
   * (used to attach a per-field badge to the seller row vs. the bank row).
   * Omit to render the overall job-wide verdict.
   */
  role?: SanctionsRole;
  size?: "sm" | "md";
}

type Severity = "green" | "yellow" | "red" | "not_checked" | "error";

function severityForRole(verdict: SanctionsVerdict, role: SanctionsRole): Severity {
  if (verdict.status === "not_checked" || verdict.status === "error") {
    return verdict.status;
  }
  const roleMatches = verdict.matches.filter((m) => m.role === role);
  if (roleMatches.length === 0) return "green";

  const confirmed = roleMatches.find(
    (m) => m.llm_verdict === "confirmed" || m.llm_verdict === "skipped"
  );
  if (confirmed) return "red";

  const unclear = roleMatches.find(
    (m) => m.llm_verdict === "unclear" || m.llm_verdict === "error"
  );
  if (unclear) return "yellow";

  // All role matches with KNOWN llm_verdict are "rejected" → clean.
  if (roleMatches.every((m) => m.llm_verdict === "rejected")) return "green";

  // Fallback: some matches have no llm_verdict at all (LLM stage didn't run
  // on this job, or the verdict was cached by an older code version). Use
  // the rapidfuzz score so the badge still warns the user instead of
  // silently showing green.
  if (roleMatches.some((m) => m.score >= 95)) return "red";
  return "yellow";
}

function labelFor(sev: Severity): string {
  switch (sev) {
    case "green":
      return "Sanctions clear";
    case "yellow":
      return "Sanctions review";
    case "red":
      return "Sanctions HIT";
    case "error":
      return "Sanctions error";
    default:
      return "Sanctions —";
  }
}

function iconFor(sev: Severity): string {
  switch (sev) {
    case "green":
      return "✓";
    case "yellow":
      return "!";
    case "red":
      return "⚠";
    case "error":
      return "×";
    default:
      return "—";
  }
}

export function SanctionsBadge({ verdict, role, size = "sm" }: Props) {
  if (!verdict) {
    return (
      <span
        className={`${styles.badge} ${styles.notChecked} ${size === "md" ? styles.md : ""}`}
        title="Sanctions screening has not run yet for this job."
      >
        <span className={styles.icon}>—</span>
        Sanctions —
      </span>
    );
  }

  const sev: Severity = role
    ? severityForRole(verdict, role)
    : (verdict.status as Severity);

  const sevClass = {
    green: styles.green,
    yellow: styles.yellow,
    red: styles.red,
    not_checked: styles.notChecked,
    error: styles.error,
  }[sev];

  const tooltipLines = [
    `Status: ${sev}`,
    verdict.checked_at ? `Checked: ${verdict.checked_at}` : "Not checked",
    verdict.list_version ? `List version: ${verdict.list_version}` : null,
    verdict.error ? `Error: ${verdict.error}` : null,
  ].filter(Boolean) as string[];

  return (
    <span
      className={`${styles.badge} ${sevClass} ${size === "md" ? styles.md : ""}`}
      title={tooltipLines.join("\n")}
    >
      <span className={styles.icon}>{iconFor(sev)}</span>
      {labelFor(sev)}
    </span>
  );
}

export default SanctionsBadge;
