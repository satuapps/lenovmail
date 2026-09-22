// Lenovmail — authored by satuapps (satuapps.com)
import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, ApiError } from "../../api";
import { fullDate, relativeTime } from "../../format";
import type { Account, AccountTest, Folder, OutboxItem } from "../../types";

const SYNC_INTERVALS = [
  { value: 60, label: "1 minute" },
  { value: 300, label: "5 minutes" },
  { value: 900, label: "15 minutes" },
  { value: 3600, label: "1 hour" },
];

const FOLDER_STATE_CHIP: Record<string, string> = {
  idle: "chip",
  syncing: "chip chip-warn",
  error: "chip chip-danger",
};

/** Single-account detail page: info, settings, folders, recent outbox, and account deletion. */
export default function AccountDetailPage() {
  const { accountId } = useParams<{ accountId: string }>();
  const navigate = useNavigate();

  const [account, setAccount] = useState<Account | null>(null);
  const [folders, setFolders] = useState<Folder[] | null>(null);
  const [outbox, setOutbox] = useState<OutboxItem[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [displayName, setDisplayName] = useState("");
  const [syncInterval, setSyncInterval] = useState(300);
  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);

  const [syncing, setSyncing] = useState(false);
  const [syncMessage, setSyncMessage] = useState<string | null>(null);

  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<AccountTest | null>(null);
  const [testError, setTestError] = useState<string | null>(null);

  const [deleting, setDeleting] = useState(false);

  const load = useCallback(async () => {
    if (!accountId) return;
    setLoadError(null);
    try {
      const [accountData, folderData, outboxData] = await Promise.all([
        api.getAccount(accountId),
        api.listFolders(accountId),
        api.listOutbox(accountId, 10),
      ]);
      setAccount(accountData);
      setFolders(folderData);
      setOutbox(outboxData);
      setDisplayName(accountData.display_name ?? "");
      setSyncInterval(accountData.sync_interval_s);
    } catch (err) {
      setLoadError(err instanceof ApiError ? err.message : "Failed to load account details.");
    }
  }, [accountId]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!accountId) {
    return <div className="p-6 text-sm text-danger">Invalid account ID.</div>;
  }

  async function handleSave() {
    setSaving(true);
    setSaveMessage(null);
    try {
      const updated = await api.updateAccount(accountId as string, {
        display_name: displayName || null,
        sync_interval_s: syncInterval,
      });
      setAccount(updated);
      setSaveMessage("Settings saved.");
    } catch (err) {
      setSaveMessage(err instanceof ApiError ? err.message : "Failed to save settings.");
    } finally {
      setSaving(false);
    }
  }

  async function handleSync() {
    setSyncing(true);
    setSyncMessage(null);
    try {
      const result = await api.syncAccount(accountId as string);
      setSyncMessage(result.status ? `Sync: ${result.status}` : "Sync scheduled.");
    } catch (err) {
      setSyncMessage(err instanceof ApiError ? err.message : "Failed to start sync.");
    } finally {
      setSyncing(false);
    }
  }

  async function handleTest() {
    setTesting(true);
    setTestError(null);
    setTestResult(null);
    try {
      setTestResult(await api.testAccount(accountId as string));
    } catch (err) {
      setTestError(err instanceof ApiError ? err.message : "Failed to test connection.");
    } finally {
      setTesting(false);
    }
  }

  async function handleDelete() {
    if (!account) return;
    if (!window.confirm(`Delete account ${account.email_address}? This cannot be undone.`)) {
      return;
    }
    setDeleting(true);
    try {
      await api.deleteAccount(accountId as string);
      navigate("/accounts");
    } catch (err) {
      setLoadError(err instanceof ApiError ? err.message : "Failed to delete account.");
      setDeleting(false);
    }
  }

  if (loadError && !account) {
    return (
      <div className="mx-auto max-w-3xl p-6">
        <div className="rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
          {loadError}
        </div>
      </div>
    );
  }

  if (!account) {
    return <div className="p-6 text-sm text-fg-muted">Loading…</div>;
  }

  return (
    <div className="mx-auto max-w-3xl p-6">
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h1 className="page-title">{account.email_address}</h1>
          <div className="mt-1 flex items-center gap-2 text-sm text-fg-muted">
            <span className="chip">{account.provider}</span>
            <span className="chip">{account.status}</span>
          </div>
        </div>
        <Link to="/accounts" className="btn btn-ghost">
          Back to list
        </Link>
      </div>

      {account.status_detail && (
        <div className="mb-4 rounded-md border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
          {account.status_detail}
        </div>
      )}

      <div className="mb-4 card">
        <div className="mb-3 text-sm font-medium text-fg">Information</div>
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
          <dt className="text-fg-muted">Last sync</dt>
          <dd className="text-fg">{relativeTime(account.last_sync_at)}</dd>
          <dt className="text-fg-muted">Unread / total</dt>
          <dd className="text-fg">
            {account.unread} / {account.total}
          </dd>
        </dl>
        <div className="mt-3 flex flex-wrap gap-2">
          <button type="button" className="btn" disabled={syncing} onClick={() => void handleSync()}>
            {syncing ? "Syncing…" : "Sync now"}
          </button>
          <button type="button" className="btn" disabled={testing} onClick={() => void handleTest()}>
            {testing ? "Testing…" : "Test connection"}
          </button>
          {account.oauth_url && (
            <button
              type="button"
              className="btn"
              onClick={() =>
                window.location.assign(`/api/oauth/microsoft/start?account_id=${accountId}`)
              }
            >
              Reconnect Microsoft
            </button>
          )}
        </div>
        {syncMessage && <div className="mt-2 text-xs text-fg-muted">{syncMessage}</div>}
        {testError && <div className="mt-2 text-xs text-danger">{testError}</div>}
        {testResult && (
          <table className="mt-3 w-full text-xs">
            <thead>
              <tr className="text-left text-fg-dim">
                <th className="pb-1">Channel</th>
                <th className="pb-1">Result</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td className="pr-2 text-fg-muted">IMAP</td>
                <td className="text-fg">{testResult.imap ?? "—"}</td>
              </tr>
              <tr>
                <td className="pr-2 text-fg-muted">SMTP</td>
                <td className="text-fg">{testResult.smtp ?? "—"}</td>
              </tr>
              <tr>
                <td className="pr-2 text-fg-muted">Graph</td>
                <td className="text-fg">{testResult.graph ?? "—"}</td>
              </tr>
              <tr>
                <td className="pr-2 text-fg-muted">Folder count</td>
                <td className="text-fg">{testResult.folders ?? "—"}</td>
              </tr>
            </tbody>
          </table>
        )}
      </div>

      <div className="mb-4 card">
        <div className="mb-3 text-sm font-medium text-fg">Settings</div>
        <div className="mb-3">
          <label className="label" htmlFor="detail-display-name">
            Display name
          </label>
          <input
            id="detail-display-name"
            className="input"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
          />
        </div>
        <div className="mb-3">
          <label className="label" htmlFor="detail-interval">
            Sync interval
          </label>
          <select
            id="detail-interval"
            className="input"
            value={syncInterval}
            onChange={(e) => setSyncInterval(Number(e.target.value))}
          >
            {SYNC_INTERVALS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>
        <button type="button" className="btn btn-primary" disabled={saving} onClick={() => void handleSave()}>
          {saving ? "Saving…" : "Save settings"}
        </button>
        {saveMessage && <div className="mt-2 text-xs text-fg-muted">{saveMessage}</div>}
      </div>

      <div className="mb-4 card">
        <div className="mb-3 text-sm font-medium text-fg">Folders</div>
        {folders && folders.length === 0 && (
          <div className="text-sm text-fg-muted">No folders synced yet.</div>
        )}
        {folders && folders.length > 0 && (
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-fg-dim">
                <th className="pb-1">Name</th>
                <th className="pb-1">Role</th>
                <th className="pb-1">Status</th>
                <th className="pb-1">Count</th>
              </tr>
            </thead>
            <tbody>
              {folders.map((folder) => (
                <tr key={folder.id} className="border-t border-ink-600">
                  <td className="py-1.5 text-fg">{folder.name}</td>
                  <td className="py-1.5 text-fg-muted">{folder.role}</td>
                  <td className="py-1.5">
                    <span className={FOLDER_STATE_CHIP[folder.sync_state] ?? "chip"}>
                      {folder.sync_state}
                    </span>
                    {folder.sync_error && (
                      <div className="mt-0.5 text-xs text-danger">{folder.sync_error}</div>
                    )}
                  </td>
                  <td className="py-1.5 text-fg-muted">
                    {folder.unread} / {folder.total}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="mb-4 card">
        <div className="mb-3 flex items-center justify-between">
          <div className="text-sm font-medium text-fg">Recent Outbox</div>
          <Link to={`/outbox?account=${accountId}`} className="text-xs text-accent hover:underline">
            View all in Outbox
          </Link>
        </div>
        {outbox && outbox.length === 0 && (
          <div className="text-sm text-fg-muted">No sent messages yet.</div>
        )}
        {outbox && outbox.length > 0 && (
          <ul className="flex flex-col gap-2 text-sm">
            {outbox.map((item) => (
              <li key={item.id} className="flex items-center justify-between border-t border-ink-600 pt-2">
                <span className="text-fg">{item.payload.subject || "(no subject)"}</span>
                <span className="flex items-center gap-2 text-xs text-fg-muted">
                  <span className="chip">{item.status}</span>
                  {fullDate(item.sent_at ?? item.created_at)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="card border-danger/40">
        <div className="mb-2 text-sm font-medium text-danger">Danger zone</div>
        <p className="mb-3 text-sm text-fg-muted">
          Deleting an account stops syncing and permanently removes related data.
        </p>
        <button type="button" className="btn btn-danger" disabled={deleting} onClick={() => void handleDelete()}>
          {deleting ? "Deleting…" : "Delete account"}
        </button>
      </div>
    </div>
  );
}
