// Lenovmail — authored by satuapps (satuapps.com)
import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, ApiError } from "../../api";
import { useEventStream } from "../../events";
import { relativeTime } from "../../format";
import type { Account, AccountTest } from "../../types";

const STATUS_CHIP: Record<string, string> = {
  active: "chip chip-ok",
  auth_error: "chip chip-danger",
  error: "chip chip-danger",
  disabled: "chip",
};

const STATUS_LABEL: Record<string, string> = {
  active: "active",
  auth_error: "auth error",
  error: "error",
  disabled: "disabled",
};

/** Per-account operation state (sync / connection test / toggle / delete) — not part of global React state. */
interface RowState {
  syncing: boolean;
  syncMessage: string | null;
  testing: boolean;
  testResult: AccountTest | null;
  testError: string | null;
  updating: boolean;
  deleting: boolean;
}

function emptyRowState(): RowState {
  return {
    syncing: false,
    syncMessage: null,
    testing: false,
    testResult: null,
    testError: null,
    updating: false,
    deleting: false,
  };
}

/** List of mail accounts with sync, connection test, enable/disable, and delete actions. */
export default function AccountsPage() {
  const navigate = useNavigate();
  const [accounts, setAccounts] = useState<Account[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [rowState, setRowState] = useState<Record<string, RowState>>({});

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const list = await api.listAccounts();
      setAccounts(list);
    } catch (err) {
      setLoadError(err instanceof ApiError ? err.message : "Failed to load accounts.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEventStream((event) => {
    if (event.type !== "account.status" && event.type !== "sync.progress") return;
    const accountId = event.payload.account_id;
    if (typeof accountId !== "string") return;
    if (!accounts?.some((a) => a.id === accountId)) return;
    void load();
  });

  function patchRow(id: string, patch: Partial<RowState>) {
    setRowState((prev) => ({ ...prev, [id]: { ...(prev[id] ?? emptyRowState()), ...patch } }));
  }

  async function handleSync(id: string) {
    patchRow(id, { syncing: true, syncMessage: null });
    try {
      const result = await api.syncAccount(id);
      patchRow(id, {
        syncing: false,
        syncMessage: result.status ? `Sync: ${result.status}` : "Sync scheduled.",
      });
    } catch (err) {
      patchRow(id, {
        syncing: false,
        syncMessage: err instanceof ApiError ? err.message : "Failed to start sync.",
      });
    }
  }

  async function handleTest(id: string) {
    patchRow(id, { testing: true, testError: null, testResult: null });
    try {
      const result = await api.testAccount(id);
      patchRow(id, { testing: false, testResult: result });
    } catch (err) {
      patchRow(id, {
        testing: false,
        testError: err instanceof ApiError ? err.message : "Failed to test connection.",
      });
    }
  }

  async function handleToggleStatus(account: Account) {
    const nextStatus = account.status === "disabled" ? "active" : "disabled";
    patchRow(account.id, { updating: true });
    try {
      await api.updateAccount(account.id, { status: nextStatus });
      await load();
    } catch (err) {
      patchRow(account.id, {
        updating: false,
        syncMessage: err instanceof ApiError ? err.message : "Failed to update account status.",
      });
      return;
    }
    patchRow(account.id, { updating: false });
  }

  async function handleDelete(account: Account) {
    if (!window.confirm(`Delete account ${account.email_address}? This cannot be undone.`)) {
      return;
    }
    patchRow(account.id, { deleting: true });
    try {
      await api.deleteAccount(account.id);
      await load();
    } catch (err) {
      patchRow(account.id, {
        deleting: false,
        syncMessage: err instanceof ApiError ? err.message : "Failed to delete account.",
      });
    }
  }

  return (
    <div className="mx-auto max-w-4xl p-6">
      <div className="mb-4 flex items-center justify-between">
        <h1 className="page-title">Mail Accounts</h1>
        <div className="flex gap-2">
          <button type="button" className="btn" onClick={() => void load()}>
            Refresh
          </button>
          <button type="button" className="btn btn-primary" onClick={() => navigate("/accounts/new")}>
            Add account
          </button>
        </div>
      </div>

      {loadError && (
        <div className="mb-4 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
          {loadError}
        </div>
      )}

      {accounts === null && !loadError && <div className="text-sm text-fg-muted">Loading…</div>}

      {accounts !== null && accounts.length === 0 && (
        <div className="card text-center text-sm text-fg-muted">
          No mail accounts yet. Click "Add account" to connect a mailbox.
        </div>
      )}

      <div className="flex flex-col gap-3">
        {accounts?.map((account) => {
          const state = rowState[account.id] ?? emptyRowState();
          return (
            <div key={account.id} className="card">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <div className="flex items-center gap-2">
                    <Link to={`/accounts/${account.id}`} className="font-medium text-fg hover:underline">
                      {account.email_address}
                    </Link>
                    <span className="chip">{account.provider}</span>
                    <span className={STATUS_CHIP[account.status] ?? "chip"}>
                      {STATUS_LABEL[account.status] ?? account.status}
                    </span>
                  </div>
                  {account.display_name && (
                    <div className="text-sm text-fg-muted">{account.display_name}</div>
                  )}
                  {account.status_detail && (
                    <div className="mt-1 text-xs text-danger">{account.status_detail}</div>
                  )}
                  <div className="mt-1 text-xs text-fg-dim">
                    {account.unread} unread / {account.total} messages · last sync{" "}
                    {relativeTime(account.last_sync_at)}
                  </div>
                </div>

                <div className="flex flex-wrap items-center gap-2">
                  {account.oauth_url && account.status !== "active" && (
                    <button
                      type="button"
                      className="btn btn-primary"
                      onClick={() => window.location.assign(account.oauth_url ?? "")}
                    >
                      Connect Microsoft
                    </button>
                  )}
                  <button
                    type="button"
                    className="btn"
                    disabled={state.syncing}
                    onClick={() => void handleSync(account.id)}
                  >
                    {state.syncing ? "Syncing…" : "Sync now"}
                  </button>
                  <button
                    type="button"
                    className="btn"
                    disabled={state.testing}
                    onClick={() => void handleTest(account.id)}
                  >
                    {state.testing ? "Testing…" : "Test connection"}
                  </button>
                  <button
                    type="button"
                    className="btn"
                    disabled={state.updating}
                    onClick={() => void handleToggleStatus(account)}
                  >
                    {account.status === "disabled" ? "Enable" : "Disable"}
                  </button>
                  <button
                    type="button"
                    className="btn btn-danger"
                    disabled={state.deleting}
                    onClick={() => void handleDelete(account)}
                  >
                    {state.deleting ? "Deleting…" : "Delete"}
                  </button>
                </div>
              </div>

              {state.syncMessage && (
                <div className="mt-3 rounded-md border border-ink-600 bg-ink-700 px-3 py-1.5 text-xs text-fg-muted">
                  {state.syncMessage}
                </div>
              )}

              {state.testError && (
                <div className="mt-3 rounded-md border border-danger/40 bg-danger/10 px-3 py-1.5 text-xs text-danger">
                  {state.testError}
                </div>
              )}

              {state.testResult && (
                <div className="mt-3 grid grid-cols-2 gap-2 rounded-md border border-ink-600 bg-ink-700 p-3 text-xs sm:grid-cols-4">
                  <div>
                    <div className="text-fg-dim">IMAP</div>
                    <div className={state.testResult.imap ? "text-fg" : "text-fg-dim"}>
                      {state.testResult.imap ?? "—"}
                    </div>
                  </div>
                  <div>
                    <div className="text-fg-dim">SMTP</div>
                    <div className={state.testResult.smtp ? "text-fg" : "text-fg-dim"}>
                      {state.testResult.smtp ?? "—"}
                    </div>
                  </div>
                  <div>
                    <div className="text-fg-dim">Graph</div>
                    <div className={state.testResult.graph ? "text-fg" : "text-fg-dim"}>
                      {state.testResult.graph ?? "—"}
                    </div>
                  </div>
                  <div>
                    <div className="text-fg-dim">Folder</div>
                    <div className="text-fg">{state.testResult.folders ?? "—"}</div>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
