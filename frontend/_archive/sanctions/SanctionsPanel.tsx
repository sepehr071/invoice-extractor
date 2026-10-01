import { useEffect, useMemo, useState } from "react";
import { rescreenJob } from "../api/client";
import { useReview } from "../state/useReview";
import { getJob } from "../api/client";
import type {
  Invoice,
  LlmVerdict,
  SanctionsAddress,
  SanctionsMatch,
  SanctionsRole,
  SanctionsVerdict,
} from "../api/types";
import styles from "./SanctionsPanel.module.css";

interface Props {
  verdict: SanctionsVerdict | null | undefined;
  /** Used to render the "what was screened" + external-verify links. */
  invoice?: Invoice | null;
  /**
   * When set, the panel body opens pre-filtered to this role (used by the
   * SanctionsBanner rollup to deep-link from a party row into its matches).
   */
  initialRoleFilter?: SanctionsRole | "all";
}

const ROLE_LABEL: Record<SanctionsRole, string> = {
  seller: "Seller",
  bank: "Bank",
  consignee: "Consignee",
  customer: "Customer",
  beneficiary: "Beneficiary",
  payable_to: "Payable-to",
};

const ROLE_CHIP_CLASS: Record<SanctionsRole, string> = {
  seller: styles.roleSeller,
  bank: styles.roleBank,
  consignee: styles.roleConsignee,
  customer: styles.roleCustomer,
  beneficiary: styles.roleBeneficiary,
  payable_to: styles.rolePayableTo,
};

/** Compact source-list display name + tint class. */
function sourceListMeta(src: string | undefined): { label: string; cls: string } | null {
  if (!src) return null;
  switch (src) {
    case "SDN":
      return { label: "SDN", cls: styles.sourceSdn };
    case "CONSOLIDATED":
      return { label: "CONS", cls: styles.sourceConsolidated };
    case "EU_CONSOLIDATED":
      return { label: "EU", cls: styles.sourceEu };
    case "UK_SANCTIONS":
      return { label: "UK", cls: styles.sourceUk };
    case "CSL_BIS_ENTITY":
      return { label: "BIS-Entity", cls: styles.sourceCslBis };
    case "CSL_BIS_DPL":
      return { label: "BIS-DPL", cls: styles.sourceCslBis };
    case "CSL_BIS_UVL":
      return { label: "BIS-UVL", cls: styles.sourceCslBis };
    case "CSL_BIS_MEU":
      return { label: "BIS-MEU", cls: styles.sourceCslBis };
    case "CSL_STATE_DDTC":
      return { label: "State-DDTC", cls: styles.sourceCslState };
    case "CSL_STATE_NONPROLIF":
      return { label: "State-NP", cls: styles.sourceCslState };
    default:
      return { label: src, cls: styles.sourceUnknown };
  }
}

/** Label for the per-match external link, derived from the source list. */
function externalLinkLabel(src: string | undefined): string {
  if (!src) return "OFAC ↗";
  if (src === "EU_CONSOLIDATED") return "EU ↗";
  if (src === "UK_SANCTIONS") return "UK ↗";
  if (src.startsWith("CSL_")) return "CSL ↗";
  // SDN, CONSOLIDATED → OFAC
  return "OFAC ↗";
}

function scriptLabel(s: string | null | undefined): string | null {
  if (!s || s === "latin") return null;
  switch (s) {
    case "arabic":
      return "Arabic";
    case "persian":
      return "Persian";
    case "chinese":
      return "Chinese";
    case "cyrillic":
      return "Cyrillic";
    default:
      return s.charAt(0).toUpperCase() + s.slice(1);
  }
}

function formatAddress(a: SanctionsAddress): string {
  const parts = [a.city, a.state, a.postal, a.country].filter(
    (p) => p != null && String(p).trim() !== ""
  ) as string[];
  return parts.join(", ");
}

/**
 * Extract the screened name + country for a role from the invoice. Mirrors
 * the backend's role-to-field mapping so banner rollup + verify footer agree.
 * Exported so SanctionsBanner can reuse it for the rollup block.
 */
export function screenedPartyFromInvoice(
  invoice: Invoice | null | undefined,
  role: SanctionsRole
): { name: string | null; country: string | null } {
  if (!invoice) return { name: null, country: null };
  switch (role) {
    case "seller":
      return {
        name: invoice.company_name ?? invoice.exporter_name ?? null,
        country: invoice.company_country ?? null,
      };
    case "bank":
      return {
        name: invoice.bank?.bank_name ?? null,
        country: invoice.bank?.bank_country ?? null,
      };
    case "consignee":
      return {
        name: invoice.consignee_name ?? null,
        country: null,
      };
    case "customer":
      return { name: invoice.customer_name ?? null, country: null };
    case "beneficiary":
      return { name: invoice.bank?.beneficiary ?? null, country: null };
    case "payable_to":
      return { name: invoice.payable_to ?? null, country: null };
  }
}

/** External self-check sources. OpenSanctions deep-links via ?q=; OFAC's own
 *  search uses a POST form so the official Treasury site only gets a landing
 *  link with the name copied into the clipboard via the title attribute. */
function buildVerificationLinks(name: string | null | undefined): {
  opensanctions: string;
  ofac: string;
} {
  const q = encodeURIComponent((name ?? "").trim());
  return {
    opensanctions: q
      ? `https://www.opensanctions.org/search/?scope=sanctions&q=${q}`
      : "https://www.opensanctions.org/datasets/us_ofac_sdn/",
    ofac: "https://sanctionssearch.ofac.treas.gov/",
  };
}

function VerifySection({
  role,
  name,
  country,
}: {
  role: string;
  name: string | null | undefined;
  country: string | null | undefined;
}) {
  if (!name || !name.trim()) {
    return (
      <div className={styles.verifyRow}>
        <span className={styles.verifyRole}>{role}:</span>
        <span className={styles.verifyMissing}>(no name extracted — nothing screened)</span>
      </div>
    );
  }
  const links = buildVerificationLinks(name);
  return (
    <div className={styles.verifyRow}>
      <span className={styles.verifyRole}>{role}:</span>
      <span className={styles.verifyName}>
        {name}
        {country && <span className={styles.verifyCountry}> · {country}</span>}
      </span>
      <a
        className={styles.verifyLink}
        href={links.opensanctions}
        target="_blank"
        rel="noopener noreferrer"
        title="Search this name on OpenSanctions (independent verification)"
      >
        OpenSanctions ↗
      </a>
      <a
        className={styles.verifyLink}
        href={links.ofac}
        target="_blank"
        rel="noopener noreferrer"
        title={`Open Treasury OFAC search — paste "${name}" into the Name field`}
      >
        Treasury OFAC ↗
      </a>
    </div>
  );
}

function llmVerdictClass(v: LlmVerdict | null): string {
  switch (v) {
    case "confirmed":
    case "skipped":
      return styles.llmConfirmed;
    case "rejected":
      return styles.llmRejected;
    case "unclear":
      return styles.llmUnclear;
    case "error":
      return styles.llmError;
    default:
      return styles.llmUnknown;
  }
}

function llmVerdictLabel(v: LlmVerdict | null): string {
  switch (v) {
    case "confirmed":
      return "LLM: same entity";
    case "skipped":
      return "Auto-confirmed (score ≥ 95)";
    case "rejected":
      return "LLM: different entity";
    case "unclear":
      return "LLM: unclear";
    case "error":
      return "LLM: error";
    default:
      return "LLM: pending";
  }
}

function buildWhySummary(verdict: SanctionsVerdict): {
  title: string;
  tone: "green" | "yellow" | "red";
  body: string;
} {
  const matches = verdict.matches ?? [];
  const total = matches.length;
  const autoConfirmed = matches.filter((m) => m.llm_verdict === "skipped").length;
  const confirmed = matches.filter((m) => m.llm_verdict === "confirmed").length;
  const rejected = matches.filter((m) => m.llm_verdict === "rejected").length;
  const unclear = matches.filter((m) => m.llm_verdict === "unclear").length;
  const llmErr = matches.filter((m) => m.llm_verdict === "error").length;
  const pending = matches.filter((m) => m.llm_verdict == null).length;
  const llmJudged = confirmed + rejected + unclear + llmErr;

  if (verdict.status === "red") {
    const parts: string[] = [];
    if (autoConfirmed > 0)
      parts.push(
        `${autoConfirmed} near-exact name match${autoConfirmed === 1 ? "" : "es"} (rapidfuzz score ≥ 95) auto-confirmed without LLM`
      );
    if (confirmed > 0)
      parts.push(
        `${confirmed} candidate${confirmed === 1 ? "" : "s"} confirmed by LLM as the same entity`
      );
    return {
      title: "Why RED",
      tone: "red",
      body:
        (parts.join("; ") || "Sanctions hit detected.") +
        ". Approval requires explicit override with a written reason (≥ 10 chars).",
    };
  }

  if (verdict.status === "yellow") {
    const parts: string[] = [];
    if (unclear > 0)
      parts.push(`${unclear} LLM verdict${unclear === 1 ? " was" : "s were"} "unclear"`);
    if (llmErr > 0)
      parts.push(
        `${llmErr} candidate${llmErr === 1 ? "" : "s"} errored during LLM judging`
      );
    if (pending > 0)
      parts.push(
        `${pending} candidate${pending === 1 ? " has" : "s have"} no LLM verdict (stage skipped)`
      );
    return {
      title: "Why YELLOW",
      tone: "yellow",
      body:
        (parts.join("; ") || "Some candidates could not be definitively cleared.") +
        ". Review each row below and decide manually before approving.",
    };
  }

  if (total === 0) {
    return {
      title: "Why GREEN",
      tone: "green",
      body: "No sanctions-list entries matched the seller or bank above the fuzzy candidate threshold (rapidfuzz ≥ 75). Nothing for the LLM to judge.",
    };
  }
  if (llmJudged > 0) {
    const allRejected = rejected === total;
    return {
      title: "Why GREEN",
      tone: "green",
      body:
        `${total} sanctions-list name${total === 1 ? "" : "s"} matched on fuzzy score, but the LLM judged ` +
        `${allRejected ? "every one" : `${rejected} of ${total}`} as a different entity ` +
        `(different countries, generic shared tokens like "Bank"/"Trading", or unrelated business types). No confirmed hit.`,
    };
  }
  return {
    title: "Why GREEN",
    tone: "green",
    body: `${total} fuzzy candidate${total === 1 ? "" : "s"} present but the LLM stage did NOT run on this job — verdict derived from rapidfuzz alone. Click "Rescreen" above to re-judge with the current LLM.`,
  };
}

function WhySummary({ verdict }: { verdict: SanctionsVerdict }) {
  const { title, tone, body } = buildWhySummary(verdict);
  const toneClass =
    tone === "red"
      ? styles.whyRed
      : tone === "yellow"
        ? styles.whyYellow
        : styles.whyGreen;
  return (
    <div className={`${styles.whyBlock} ${toneClass}`}>
      <span className={styles.whyLabel}>{title}</span>
      <span className={styles.whyBody}>{body}</span>
    </div>
  );
}

function MatchRow({ match }: { match: SanctionsMatch }) {
  const scoreClass =
    match.score >= 95 ? styles.scoreRed : match.score >= 85 ? styles.scoreYellow : "";
  const verdictClass = llmVerdictClass(match.llm_verdict);
  const roleChipCls = ROLE_CHIP_CLASS[match.role] ?? styles.roleSeller;
  const source = sourceListMeta(match.source_list);
  const script = scriptLabel(match.matched_script);
  const addresses = match.addresses ?? [];
  const visibleAddrs = addresses.slice(0, 3);
  const hiddenAddrCount = Math.max(0, addresses.length - visibleAddrs.length);

  return (
    <div className={`${styles.matchRow} ${verdictClass}`}>
      <div className={styles.matchHeader}>
        <span className={`${styles.roleChip} ${roleChipCls}`}>
          {ROLE_LABEL[match.role] ?? match.role}
        </span>
        {source && (
          <span
            className={`${styles.sourcePill} ${source.cls}`}
            title={`Source list: ${match.source_list}`}
          >
            {source.label}
          </span>
        )}
        {script && (
          <span
            className={styles.scriptChip}
            title="matched via native-script alias"
          >
            {script}
          </span>
        )}
        <span className={`${styles.scorePill} ${scoreClass}`} title="rapidfuzz score (stage 1)">
          {match.score.toFixed(1)}
        </span>
        <span className={`${styles.verdictChip} ${verdictClass}`}>
          {llmVerdictLabel(match.llm_verdict)}
        </span>
        <span className={styles.sdnType}>{match.sdn_type}</span>
        <a
          className={styles.externalLink}
          href={match.treasury_url}
          target="_blank"
          rel="noopener noreferrer"
          title="Open the official source listing for this entry"
        >
          {externalLinkLabel(match.source_list)}
        </a>
      </div>

      <div className={styles.matchedName}>{match.matched_name}</div>
      {match.matched_name !== match.primary_name && (
        <div className={styles.primaryName}>aka: {match.primary_name}</div>
      )}

      {match.llm_reasoning && (
        <div className={styles.llmReasoning}>
          <span className={styles.llmReasoningLabel}>LLM reasoning:</span> {match.llm_reasoning}
        </div>
      )}

      {match.programs.length > 0 && (
        <div className={styles.programList}>
          {match.programs.map((p) => (
            <span key={p} className={styles.programChip} title="Sanctions program">
              {p}
            </span>
          ))}
        </div>
      )}

      {addresses.length > 0 ? (
        <div className={styles.addressList}>
          <span className={styles.metaLabel}>Addresses:</span>
          {visibleAddrs.map((a, i) => {
            const txt = formatAddress(a);
            if (!txt) return null;
            return (
              <div key={i} className={styles.addressRow}>
                {txt}
              </div>
            );
          })}
          {hiddenAddrCount > 0 && (
            <div className={styles.addressMore}>+{hiddenAddrCount} more</div>
          )}
        </div>
      ) : (
        match.address_countries.length > 0 && (
          <div className={styles.countryList}>
            <span className={styles.metaLabel}>Countries:</span>
            {match.address_countries.join(", ")}
          </div>
        )
      )}
    </div>
  );
}

/**
 * Body-only export — banner-owner renders this inside its expanded region.
 * NO outer .panel wrapper, NO header-button toggle. Contains: meta strip
 * w/ rescreen button, WhySummary, hide-rejected toggle, match list, verify
 * footer.
 */
export function SanctionsPanelBody({ verdict, invoice, initialRoleFilter }: Props) {
  const [showRejected, setShowRejected] = useState(true);
  const [verifyOpen, setVerifyOpen] = useState(false);
  const [rescreening, setRescreening] = useState(false);
  const [rescreenError, setRescreenError] = useState<string | null>(null);
  const [roleFilter, setRoleFilter] = useState<SanctionsRole | "all">(
    initialRoleFilter ?? "all"
  );
  // Sync filter when caller deep-links to a role (rollup click in banner).
  useEffect(() => {
    if (initialRoleFilter) setRoleFilter(initialRoleFilter);
  }, [initialRoleFilter]);
  const jobId = useReview((s) => s.jobId);
  const setDetail = useReview((s) => s.setDetail);

  const onRescreen = async () => {
    if (!jobId || rescreening) return;
    setRescreening(true);
    setRescreenError(null);
    try {
      await rescreenJob(jobId);
      const fresh = await getJob(jobId);
      setDetail(fresh);
    } catch (e) {
      setRescreenError((e as Error).message);
    } finally {
      setRescreening(false);
    }
  };

  const grouped = useMemo(() => {
    const matches = verdict?.matches ?? [];
    return {
      confirmed: matches.filter(
        (m) => m.llm_verdict === "confirmed" || m.llm_verdict === "skipped"
      ),
      unclear: matches.filter(
        (m) => m.llm_verdict === "unclear" || m.llm_verdict === "error"
      ),
      rejected: matches.filter((m) => m.llm_verdict === "rejected"),
      pending: matches.filter((m) => m.llm_verdict === null || m.llm_verdict === undefined),
    };
  }, [verdict]);

  // Distinct roles that actually appear in matches — drives the filter chip row.
  const rolesPresent = useMemo<SanctionsRole[]>(() => {
    const seen = new Set<SanctionsRole>();
    for (const m of verdict?.matches ?? []) seen.add(m.role);
    // Stable order matching the doc-spec.
    const order: SanctionsRole[] = [
      "seller",
      "bank",
      "consignee",
      "customer",
      "beneficiary",
      "payable_to",
    ];
    return order.filter((r) => seen.has(r));
  }, [verdict]);

  // If the chosen role-filter no longer has any matches (e.g. after rescreen),
  // silently fall back to "all".
  useEffect(() => {
    if (roleFilter !== "all" && !rolesPresent.includes(roleFilter)) {
      setRoleFilter("all");
    }
  }, [rolesPresent, roleFilter]);

  if (!verdict) return null;

  // Neutral / error statuses: minimal one-line body, no meta/why/match list.
  if (verdict.status === "not_checked") {
    return (
      <div className={styles.headerNeutral}>
        <strong>Sanctions screening — not checked.</strong> Download the screening
        lists from the upload page to enable screening on future jobs.
      </div>
    );
  }
  if (verdict.status === "error") {
    return (
      <div className={styles.headerError}>
        <strong>Sanctions screening failed.</strong>{" "}
        {verdict.error || "Unknown error during sanctions phase."}
      </div>
    );
  }

  const allOrdered = showRejected
    ? [...grouped.confirmed, ...grouped.unclear, ...grouped.pending, ...grouped.rejected]
    : [...grouped.confirmed, ...grouped.unclear, ...grouped.pending];
  const visibleMatches =
    roleFilter === "all"
      ? allOrdered
      : allOrdered.filter((m) => m.role === roleFilter);

  return (
    <>
      <div className={styles.meta}>
        {verdict.list_version && (
          <>
            List version: <strong>{verdict.list_version}</strong>
            {" · "}
          </>
        )}
        {verdict.checked_at && (
          <>
            checked <strong>{verdict.checked_at.replace("T", " ").slice(0, 19)}</strong>
          </>
        )}
        {verdict.llm_model ? (
          <>
            {" · LLM "}
            <strong>{verdict.llm_model}</strong>
          </>
        ) : (
          <span className={styles.staleVerdict}>
            {" · LLM stage did NOT run on this job — verdict is rapidfuzz-only"}
          </span>
        )}
        {verdict.llm_error && <span className={styles.llmErrorBanner}> · LLM error: {verdict.llm_error}</span>}
        <button
          type="button"
          className={styles.rescreenBtn}
          onClick={onRescreen}
          disabled={rescreening}
          title="Re-run sanctions screening on this job against the current downloaded lists"
        >
          {rescreening ? "Rescreening…" : "↻ Rescreen"}
        </button>
        {rescreenError && <div className={styles.rescreenError}>{rescreenError}</div>}
      </div>

      <WhySummary verdict={verdict} />

      {rolesPresent.length > 1 && (
        <div className={styles.roleFilter}>
          <span className={styles.roleFilterLabel}>Filter:</span>
          <button
            type="button"
            className={`${styles.roleFilterChip} ${roleFilter === "all" ? styles.roleFilterChipActive : ""}`}
            onClick={() => setRoleFilter("all")}
          >
            All ({verdict.matches?.length ?? 0})
          </button>
          {rolesPresent.map((r) => {
            const count = (verdict.matches ?? []).filter((m) => m.role === r).length;
            return (
              <button
                type="button"
                key={r}
                className={`${styles.roleFilterChip} ${roleFilter === r ? styles.roleFilterChipActive : ""}`}
                onClick={() => setRoleFilter(r)}
              >
                {ROLE_LABEL[r]} ({count})
              </button>
            );
          })}
        </div>
      )}

      {grouped.rejected.length > 0 && (
        <div className={styles.toggleRow}>
          <button
            type="button"
            className={styles.toggleBtn}
            onClick={() => setShowRejected((v) => !v)}
          >
            {showRejected
              ? `Hide ${grouped.rejected.length} LLM-rejected match${grouped.rejected.length > 1 ? "es" : ""}`
              : `Show ${grouped.rejected.length} LLM-rejected match${grouped.rejected.length > 1 ? "es" : ""}`}
          </button>
        </div>
      )}

      {visibleMatches.length > 0 && (
        <div className={styles.matchList}>
          {visibleMatches.map((m, i) => (
            <MatchRow
              key={`${m.role}-${m.source_list ?? "SDN"}-${m.uid}-${i}`}
              match={m}
            />
          ))}
        </div>
      )}

      {/* Self-verify footer — always available so user can audit ANY status. */}
      <div className={styles.verifyBlock}>
        <button
          type="button"
          className={styles.verifyToggle}
          onClick={() => setVerifyOpen((v) => !v)}
          aria-expanded={verifyOpen}
        >
          <span className={styles.verifyIcon}>🔎</span>
          {verifyOpen ? "Hide self-verification" : "Double-check yourself"}
          <span className={styles.verifyChevron}>{verifyOpen ? "▾" : "▸"}</span>
        </button>
        {verifyOpen && (
          <div className={styles.verifyBody}>
            <p className={styles.verifyHelp}>
              Names exactly as screened by this app. Click a source to verify
              independently — recommended for "green" results before approving
              a high-value transaction.
            </p>
            {(
              [
                "seller",
                "bank",
                "consignee",
                "customer",
                "beneficiary",
                "payable_to",
              ] as SanctionsRole[]
            ).map((r) => {
              const { name, country } = screenedPartyFromInvoice(invoice, r);
              // Skip empty rows for non-essential roles — keeps footer focused.
              if (!name && r !== "seller" && r !== "bank") return null;
              return (
                <VerifySection
                  key={r}
                  role={ROLE_LABEL[r]}
                  name={name}
                  country={country}
                />
              );
            })}
            <div className={styles.verifyMeta}>
              Sources: <strong>OpenSanctions</strong> (aggregated OFAC + EU + UN + UK
              + PEPs; deep-links your query) · <strong>Treasury OFAC</strong> (official US
              SDN search — paste name manually).
            </div>
          </div>
        )}
      </div>
    </>
  );
}

/**
 * Legacy wrapper kept for back-compat callers that still render the full
 * panel (header button + body) directly. New surface is `SanctionsBanner`
 * which composes `SanctionsPanelBody` inside its slide-out region.
 */
export function SanctionsPanel({ verdict, invoice }: Props) {
  const [expanded, setExpanded] = useState(true);

  if (!verdict) return null;
  if (verdict.status === "not_checked" || verdict.status === "error") {
    return (
      <div className={styles.panel}>
        <SanctionsPanelBody verdict={verdict} invoice={invoice} />
      </div>
    );
  }

  const isRed = verdict.status === "red";
  const isYellow = verdict.status === "yellow";
  const isGreen = verdict.status === "green";
  const headerClass = isRed
    ? styles.headerRed
    : isYellow
      ? styles.headerYellow
      : styles.headerGreen;

  // Recompute summary bits to keep legacy header label stable.
  const matches = verdict.matches ?? [];
  const confirmedCt = matches.filter(
    (m) => m.llm_verdict === "confirmed" || m.llm_verdict === "skipped"
  ).length;
  const unclearCt = matches.filter(
    (m) => m.llm_verdict === "unclear" || m.llm_verdict === "error"
  ).length;
  const rejectedCt = matches.filter((m) => m.llm_verdict === "rejected").length;

  const summaryBits: string[] = [];
  if (confirmedCt > 0) summaryBits.push(`${confirmedCt} confirmed`);
  if (unclearCt > 0) summaryBits.push(`${unclearCt} unclear`);
  if (rejectedCt > 0) summaryBits.push(`${rejectedCt} rejected by LLM`);
  if (isGreen && summaryBits.length === 0)
    summaryBits.push("0 candidates above threshold");

  return (
    <div className={styles.panel}>
      <button
        type="button"
        className={`${styles.header} ${headerClass}`}
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        <span className={styles.headerIcon}>{isRed ? "⚠" : isYellow ? "!" : "✓"}</span>
        <span className={styles.headerText}>
          OFAC {isRed ? "HIT" : isYellow ? "Review" : "cleared by LLM"} — {summaryBits.join(" · ")}
        </span>
        <span className={styles.headerChevron}>{expanded ? "▾" : "▸"}</span>
      </button>

      {expanded && <SanctionsPanelBody verdict={verdict} invoice={invoice} />}
    </div>
  );
}

export default SanctionsPanel;
