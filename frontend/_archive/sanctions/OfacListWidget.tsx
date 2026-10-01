import { useCallback, useEffect, useState } from "react";
import { getSanctionsStatus, refreshSanctionsList } from "../api/client";
import type {
  SanctionsListRow,
  SanctionsListStatus,
  SanctionsRefreshResult,
} from "../api/types";
import styles from "./OfacListWidget.module.css";

const STALE_SECONDS = 7 * 86400;

function humanizeAge(seconds: number | null | undefined): string {
  if (seconds == null) return "never";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

/** Build a one-line toast summarizing the per-list refresh outcome. */
function summarizeRefresh(lists: Record<string, SanctionsRefreshResult>): string {
  const entries = Object.entries(lists);
  const ok = entries.filter(([, r]) => r.ok).map(([k]) => k);
  const failed = entries.filter(([, r]) => !r.ok && !r.skipped).map(([k]) => k);
  const skipped = entries.filter(([, r]) => !r.ok && r.skipped).map(([k]) => k);

  const parts: string[] = [];
  if (ok.length) parts.push(`${ok.length} updated (${ok.join(", ")})`);
  if (failed.length) parts.push(`${failed.length} failed (${failed.join(", ")})`);
  if (skipped.length) parts.push(`${skipped.length} skipped`);
  return parts.length ? `Sanctions lists: ${parts.join(" · ")}.` : "Sanctions refresh complete.";
}

function ListRow({
  row,
  refresh,
}: {
  row: SanctionsListRow;
  refresh: SanctionsRefreshResult | undefined;
}) {
  const ready = row.available;
  const isStale = row.age_seconds != null && row.age_seconds > STALE_SECONDS;
  const toneClass = !row.enabled
    ? styles.rowDisabled
    : ready
      ? styles.rowReady
      : styles.rowNotReady;

  return (
    <div className={`${styles.row} ${toneClass}`}>
      <div className={styles.rowMain}>
        <span className={styles.rowTitle}>
          <span
            className={`${styles.dot} ${
              !row.enabled ? styles.dotDisabled : ready ? styles.dotReady : styles.dotNotReady
            }`}
          />
          {row.label}
          {!row.enabled && <span className={styles.disabledTag}>(disabled)</span>}
        </span>
        <span className={styles.rowAuthority}>{row.authority}</span>
      </div>

      <div className={styles.rowStats}>
        <span className={styles.statValue}>
          {ready ? row.entry_count.toLocaleString() : "—"}
          <span className={styles.statUnit}> entries</span>
        </span>
        <span
          className={`${styles.statValue} ${isStale ? styles.stale : ""}`}
          title={row.fetched_at ?? undefined}
        >
          {ready ? humanizeAge(row.age_seconds) : "never"}
        </span>
        {row.list_version && (
          <span className={styles.statVersion} title="List version">
            {row.list_version}
          </span>
        )}
      </div>

      {row.enabled && !ready && (
        <div className={styles.rowHint}>Not downloaded yet — refresh to enable screening.</div>
      )}
      {row.enabled && ready && isStale && (
        <div className={styles.rowStale}>More than 7 days old — refresh before screening.</div>
      )}
      {row.last_error && <div className={styles.rowError}>Last error: {row.last_error}</div>}
      {refresh && !refresh.ok && !refresh.skipped && refresh.error && (
        <div className={styles.rowError}>Refresh failed: {refresh.error}</div>
      )}
    </div>
  );
}

export function OfacListWidget() {
  const [status, setStatus] = useState<SanctionsListStatus | null>(null);
  const [refreshResults, setRefreshResults] = useState<
    Record<string, SanctionsRefreshResult>
  >({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      const s = await getSanctionsStatus();
      setStatus(s);
    } catch (e) {
      setError(`Status fetch failed: ${(e as Error).message}`);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const onRefresh = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const result = await refreshSanctionsList();
      setRefreshResults(result.lists ?? {});
      setToast(summarizeRefresh(result.lists ?? {}));
      window.setTimeout(() => setToast(null), 6000);
      await reload();
    } catch (e) {
      setError(`Refresh failed: ${(e as Error).message}`);
    } finally {
      setBusy(false);
    }
  };

  const lists = status?.lists ?? [];
  const anyAvailable = lists.some((l) => l.enabled && l.available);
  const total = status?.total_entry_count ?? 0;

  return (
    <div className={`${styles.widget} ${anyAvailable ? styles.ready : styles.notReady}`}>
      <div className={styles.headerRow}>
        <span className={styles.title}>
          <span className={styles.dot} />
          Sanctions lists
          {total > 0 && (
            <span className={styles.totalTag}>{total.toLocaleString()} total entries</span>
          )}
        </span>
        <button
          type="button"
          className={styles.refreshBtn}
          onClick={onRefresh}
          disabled={busy}
        >
          {busy ? "Downloading…" : "Refresh all"}
        </button>
      </div>

      {lists.length > 0 ? (
        <div className={styles.list}>
          {lists.map((row) => (
            <ListRow key={row.key} row={row} refresh={refreshResults[row.key]} />
          ))}
        </div>
      ) : (
        !error && <div className={styles.loading}>Loading list status…</div>
      )}

      {!anyAvailable && lists.length > 0 && (
        <div className={styles.warning}>
          No enabled list is downloaded — sanctions screening will report{" "}
          <code>not_checked</code> on every job until you refresh.
        </div>
      )}

      <div className={styles.statusRegion} aria-live="polite">
        {error && <div className={styles.error}>{error}</div>}
        {toast && <div className={styles.toast}>{toast}</div>}
      </div>
    </div>
  );
}

export default OfacListWidget;
