// Lenovmail — authored by satuapps (satuapps.com)
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError } from "../../api";
import { useEventStream } from "../../events";
import { relativeTime } from "../../format";
import type { Account, OpsSummary } from "../../types";
import { daysUntil } from "../mail/mailUtils";

/**
 * Operations view. `error` and `auth_error` are deliberately two sections rather than one
 * "problem accounts" list: an unreachable server is retried automatically and a retry button
 * is useful, while refused credentials will refuse again until a human re-authenticates.
 */
export default function OpsPage() {
  const [summary, setSummary] = useState<OpsSummary | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyIds, setBusyIds] = useState<Record<string, boolean>>({});
  const [results, setResults] = useState<Record<string, string>>({});
  const [retryingAll, setRetryingAll] = useState(false);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      setSummary(await api.opsSummary());
    } catch (err) {
      setLoadError(
        err instanceof ApiError ? err.message : "Failed to load ops summary.",
      );
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEventStream((event) => {
    if (event.type === "account.status") void load();
  });

  async function retry(id: string) {
    setBusyIds((prev) => ({ ...prev, [id]: true }));
    try {
      const result = await api.syncAccount(id);
      setResults((prev) => ({
        ...prev,
        [id]: result.status ? `Sync: ${result.status}` : "Sync scheduled.",
      }));
    } catch (err) {
      setResults((prev) => ({
        ...prev,
        [id]: err instanceof ApiError ? err.message : "Failed to start sync.",
      }));
    } finally {
      setBusyIds((prev) => ({ ...prev, [id]: false }));
    }
  }

  async function retryAll(accounts: Account[]) {
    setRetryingAll(true);
    // Sequential on purpose: a long unreachable list should not flood the sync queue.
    for (const account of accounts) await retry(account.id);
    setRetryingAll(false);
  }

  const janitor = summary?.janitor;
  const nextRetireDays = daysUntil(janitor?.next_retire_at ?? null);

  return (
    <div className="mx-auto max-w-4xl p-6">
      <div className="mb-4 flex items-center justify-between">
        <h1 className="page-title">Ops</h1>
        <button type="button" className="btn" onClick={() => void load()}>
          Refresh
        </button>
      </div>

      {loadError && (
        <div className="mb-4 flex items-center gap-3">
          <span className="chip chip-danger">{loadError}</span>
          <button type="button" className="btn" onClick={() => void load()}>
            Retry
          </button>
        </div>
      )}

      {summary === null && !loadError && (
        <div className="text-sm text-fg-muted">Loading…</div>
      )}

      {summary !== null && (
        <div className="flex flex-col gap-4">
          <section className="panel rounded-xl p-4">
            <div className="mb-1 flex items-center justify-between gap-3">
              <h2 className="page-title">
                Unreachable — server did not answer
              </h2>
              {summary.unreachable.length > 0 && (
                <button
                  type="button"
                  className="btn"
                  disabled={retryingAll}
                  onClick={() => void retryAll(summary.unreachable)}
                >
                  {retryingAll ? "Retrying…" : "Retry all"}
                </button>
              )}
            </div>
            <p className="mb-3 text-xs text-fg-dim">
              Still in the sync rotation — the next scheduled run may well
              succeed on its own.
            </p>
            {summary.unreachable.length === 0 ? (
              <div className="text-sm text-fg-muted">
                No accounts in this state.
              </div>
            ) : (
              <div className="flex flex-col gap-2">
                {summary.unreachable.map((account) => (
                  <div
                    key={account.id}
                    className="flex flex-wrap items-center justify-between gap-2 border-t border-ink-600 pt-2"
                  >
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <Link
                          to={`/accounts/${account.id}`}
                          className="text-fg hover:underline"
                        >
                          {account.email_address}
                        </Link>
                        <span className="chip chip-warn">unreachable</span>
                        <span className="chip">{account.provider}</span>
                      </div>
                      {account.status_detail && (
                        <div
                          className="truncate text-xs text-fg-muted"
                          title={account.status_detail}
                        >
                          {account.status_detail}
                        </div>
                      )}
                      <div className="text-xs text-fg-dim">
                        last sync {relativeTime(account.last_sync_at)}
                        {results[account.id] ? ` · ${results[account.id]}` : ""}
                      </div>
                    </div>
                    <button
                      type="button"
                      className="btn"
                      disabled={busyIds[account.id] ?? false}
                      onClick={() => void retry(account.id)}
                    >
                      {busyIds[account.id] ? "Retrying…" : "Retry sync"}
                    </button>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className="panel rounded-xl p-4">
            <h2 className="page-title">Auth rejected — credentials refused</h2>
            <p className="mb-3 text-xs text-fg-dim">
              Retrying will not help; re-authenticate before the janitor retires
              the account.
            </p>
            {summary.auth_rejected.length === 0 ? (
              <div className="text-sm text-fg-muted">
                No accounts in this state.
              </div>
            ) : (
              <div className="flex flex-col gap-2">
                {summary.auth_rejected.map((account) => {
                  const days = daysUntil(account.retire_at);
                  return (
                    <div
                      key={account.id}
                      className="flex flex-wrap items-center justify-between gap-2 border-t border-ink-600 pt-2"
                    >
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <Link
                            to={`/accounts/${account.id}`}
                            className="text-fg hover:underline"
                          >
                            {account.email_address}
                          </Link>
                          <span className="chip chip-danger">auth reject</span>
                          <span className="chip">{account.provider}</span>
                          {days !== null && (
                            <span
                              className={
                                account.retire_warning
                                  ? "chip chip-warn"
                                  : "chip"
                              }
                            >
                              {account.retire_action ?? "retire"} in {days}d
                            </span>
                          )}
                        </div>
                        {account.status_detail && (
                          <div
                            className="truncate text-xs text-fg-muted"
                            title={account.status_detail}
                          >
                            {account.status_detail}
                          </div>
                        )}
                        <div className="text-xs text-fg-dim">
                          rejected since {relativeTime(account.invalid_since)}
                        </div>
                      </div>
                      <div className="flex items-center gap-2">
                        {account.oauth_url && (
                          <a
                            className="btn btn-primary"
                            href={account.oauth_url}
                          >
                            Connect Microsoft
                          </a>
                        )}
                        <Link to={`/accounts/${account.id}`} className="btn">
                          Fix credentials
                        </Link>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </section>

          <section className="panel rounded-xl p-4">
            <h2 className="page-title">Provider quota &amp; rate</h2>
            <p className="mb-3 text-xs text-fg-dim">
              Observed throughput next to the configured caps. Display only — no
              limit is enforced here.
            </p>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-[11px] uppercase tracking-[0.14em] text-fg-dim">
                  <tr>
                    <th className="px-3 py-2 font-medium">Provider</th>
                    <th className="px-3 py-2 font-medium">Accounts</th>
                    <th className="px-3 py-2 font-medium">Sync 24h</th>
                    <th className="px-3 py-2 font-medium">Failed 24h</th>
                    <th className="px-3 py-2 font-medium">Sent / hour</th>
                    <th className="px-3 py-2 font-medium">Conns / account</th>
                    <th className="px-3 py-2 font-medium">Poll</th>
                  </tr>
                </thead>
                <tbody className="mono">
                  {summary.providers.map((row) => (
                    <tr key={row.provider} className="border-t border-ink-600">
                      <td className="px-3 py-2 text-fg">{row.provider}</td>
                      <td className="px-3 py-2 text-fg-muted">
                        {row.accounts_total}
                      </td>
                      <td className="px-3 py-2 text-fg-muted">
                        {row.sync_runs_24h}
                      </td>
                      <td
                        className={
                          row.sync_failures_24h > 0
                            ? "px-3 py-2 text-warn"
                            : "px-3 py-2 text-fg-muted"
                        }
                      >
                        {row.sync_failures_24h}
                      </td>
                      <td className="px-3 py-2 text-fg-muted">
                        {row.sent_last_hour} / {row.send_limit_per_hour}
                      </td>
                      <td className="px-3 py-2 text-fg-muted">
                        {row.max_connections_per_account ?? "—"}
                      </td>
                      <td className="px-3 py-2 text-fg-muted">
                        {row.poll_interval_s === null
                          ? "—"
                          : `${row.poll_interval_s}s`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {janitor && (
              <div className="mt-3 border-t border-ink-600 pt-3 text-xs text-fg-dim">
                Janitor: {janitor.action} after {janitor.grace_days}d of auth
                failure · batch {janitor.check_batch} · tokens kept{" "}
                {janitor.token_retention_days}d · audit kept{" "}
                {janitor.audit_retention_days}d · next retirement{" "}
                {/* A future date: `relativeTime` is past-tense and would read "just now". */}
                {nextRetireDays === null
                  ? "none scheduled"
                  : `in ${nextRetireDays}d`}
              </div>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
