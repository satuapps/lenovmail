// Lenovmail — authored by satuapps (satuapps.com)
// `/outbox` page: single-account send queue, with approval for `pending_approval` items.
import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, ApiError } from "../../api";
import type { OutboxItem } from "../../types";
import { relativeTime, fullDate } from "../../format";
import { useEventStream } from "../../events";
import { useAccounts } from "./mailHooks";
import { OUTBOX_STATUS_LABEL, outboxStatusChipClass } from "./mailUtils";

export default function OutboxPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { accounts, loading: accountsLoading } = useAccounts();
  const accountId = searchParams.get("account");

  const [items, setItems] = useState<OutboxItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [approvingId, setApprovingId] = useState<string | null>(null);

  useEffect(() => {
    if (accountId !== null || accounts.length === 0) return;
    const first = accounts[0];
    if (first === undefined) return;
    const next = new URLSearchParams(searchParams);
    next.set("account", first.id);
    setSearchParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountId, accounts]);

  const reload = useCallback(() => {
    if (accountId === null) {
      setItems([]);
      return;
    }
    setLoading(true);
    setError(null);
    api
      .listOutbox(accountId)
      .then(setItems)
      .catch((err: unknown) => {
        setError(err instanceof ApiError ? err.message : "Failed to load outbox");
      })
      .finally(() => setLoading(false));
  }, [accountId]);

  useEffect(() => {
    reload();
  }, [reload]);

  useEventStream((event) => {
    const eventAccountId = typeof event.payload.account_id === "string" ? event.payload.account_id : null;
    if (eventAccountId !== null && eventAccountId === accountId) reload();
  });

  const handleApprove = (id: string) => {
    setApprovingId(id);
    setError(null);
    api
      .approveOutbox(id)
      .then((updated) => {
        setItems((prev) => prev.map((item) => (item.id === id ? updated : item)));
      })
      .catch((err: unknown) => {
        setError(err instanceof ApiError ? err.message : "Failed to approve send");
      })
      .finally(() => setApprovingId(null));
  };

  return (
    <div className="flex h-full min-h-0 flex-col p-4">
      <div className="mb-4 flex items-center gap-3">
        <h1 className="text-lg font-semibold text-fg">Outbox</h1>
        <select
          className="input w-auto"
          value={accountId ?? ""}
          disabled={accountsLoading || accounts.length === 0}
          onChange={(event) => {
            const next = new URLSearchParams(searchParams);
            next.set("account", event.target.value);
            setSearchParams(next);
          }}
        >
          {accounts.length === 0 && <option value="">No accounts yet</option>}
          {accounts.map((account) => (
            <option key={account.id} value={account.id}>
              {account.display_name || account.email_address}
            </option>
          ))}
        </select>
      </div>

      {error !== null && <p className="mb-3 text-sm text-danger">{error}</p>}

      <div className="min-h-0 flex-1 overflow-y-auto rounded-lg border border-ink-600">
        <table className="w-full border-collapse text-sm">
          <thead className="sticky top-0 bg-ink-800 text-left text-xs uppercase tracking-wide text-fg-muted">
            <tr>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2">Subject</th>
              <th className="px-3 py-2">Recipient</th>
              <th className="px-3 py-2">Created</th>
              <th className="px-3 py-2">Sent</th>
              <th className="px-3 py-2">Attempts</th>
              <th className="px-3 py-2">Error</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr>
                <td colSpan={8} className="p-4 text-center text-fg-muted">
                  Loading…
                </td>
              </tr>
            )}
            {!loading && items.length === 0 && (
              <tr>
                <td colSpan={8} className="p-4 text-center text-fg-muted">
                  No mail in the outbox yet.
                </td>
              </tr>
            )}
            {items.map((item) => (
              <tr key={item.id} className="border-t border-ink-600">
                <td className="px-3 py-2">
                  <span className={outboxStatusChipClass(item.status)}>
                    {OUTBOX_STATUS_LABEL[item.status] ?? item.status}
                  </span>
                </td>
                <td className="max-w-xs truncate px-3 py-2 text-fg">{item.payload.subject || "(no subject)"}</td>
                <td className="max-w-xs truncate px-3 py-2 text-fg-muted">{item.payload.to.join(", ")}</td>
                <td className="px-3 py-2 text-fg-muted" title={fullDate(item.created_at)}>
                  {relativeTime(item.created_at)}
                </td>
                <td className="px-3 py-2 text-fg-muted">{fullDate(item.sent_at)}</td>
                <td className="px-3 py-2 text-fg-muted">{item.attempts}</td>
                <td className="max-w-xs truncate px-3 py-2 text-danger" title={item.last_error ?? undefined}>
                  {item.last_error ?? ""}
                </td>
                <td className="px-3 py-2">
                  {item.status === "pending_approval" && (
                    <button
                      type="button"
                      className="btn btn-primary"
                      disabled={approvingId === item.id}
                      onClick={() => handleApprove(item.id)}
                    >
                      {approvingId === item.id ? "Approving…" : "Approve"}
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
