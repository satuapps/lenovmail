// Lenovmail — authored by satuapps (satuapps.com)
// MailPage left sidebar: account picker + folder list for the selected account.
import type { Account, Folder } from "../../types";
import { accountStatusChipClass, folderIcon, folderLabel, syncStateDotClass } from "./mailUtils";

interface FolderSidebarProps {
  accounts: Account[];
  accountsLoading: boolean;
  activeAccountId: string | null;
  onSelectAccount: (accountId: string) => void;
  folders: Folder[];
  foldersLoading: boolean;
  activeFolderId: string | null;
  onSelectFolder: (folderId: string | null) => void;
}

export default function FolderSidebar({
  accounts,
  accountsLoading,
  activeAccountId,
  onSelectAccount,
  folders,
  foldersLoading,
  activeFolderId,
  onSelectFolder,
}: FolderSidebarProps) {
  const activeAccount = accounts.find((account) => account.id === activeAccountId) ?? null;

  return (
    <aside className="flex h-full min-h-0 w-64 shrink-0 flex-col border-r border-ink-600 bg-ink-800">
      <div className="border-b border-ink-600 p-2">
        <label className="label" htmlFor="mail-account-select">
          Account
        </label>
        <select
          id="mail-account-select"
          className="input"
          value={activeAccountId ?? ""}
          disabled={accountsLoading || accounts.length === 0}
          onChange={(event) => onSelectAccount(event.target.value)}
        >
          {accounts.length === 0 && <option value="">No accounts yet</option>}
          {accounts.map((account) => (
            <option key={account.id} value={account.id}>
              {account.display_name || account.email_address}
            </option>
          ))}
        </select>
        {activeAccount !== null && (
          <div className="mt-2 flex items-center justify-between text-xs text-fg-muted">
            <span className="mono truncate">{activeAccount.email_address}</span>
            <span className={accountStatusChipClass(activeAccount.status)}>{activeAccount.status}</span>
          </div>
        )}
      </div>
      <nav className="min-h-0 flex-1 overflow-y-auto p-2">
        <p className="label px-2 pt-1">Folders</p>
        <button
          type="button"
          className={`flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left text-sm ${
            activeFolderId === null ? "row-active" : "text-fg-muted hover:bg-white/[0.05] hover:text-fg"
          }`}
          onClick={() => onSelectFolder(null)}
        >
          <span className="flex items-center gap-2">
            <span aria-hidden>{"\u2709\uFE0F"}</span>
            All Mail
          </span>
        </button>
        {foldersLoading && <p className="px-2 py-2 text-xs text-fg-dim">Loading folders…</p>}
        {!foldersLoading && folders.length === 0 && activeAccountId !== null && (
          <p className="px-2 py-2 text-xs text-fg-dim">No folders yet.</p>
        )}
        <ul className="mt-1 space-y-0.5">
          {folders.map((folder) => (
            <li key={folder.id}>
              <button
                type="button"
                className={`flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-sm ${
                  activeFolderId === folder.id
                    ? "row-active"
                    : "text-fg-muted hover:bg-white/[0.05] hover:text-fg"
                }`}
                onClick={() => onSelectFolder(folder.id)}
                title={folder.sync_error ?? undefined}
              >
                <span className="flex min-w-0 items-center gap-2">
                  <span
                    className={`h-1.5 w-1.5 shrink-0 rounded-full ${syncStateDotClass(folder.sync_state)}`}
                    aria-hidden
                  />
                  <span aria-hidden>{folderIcon(folder.role)}</span>
                  <span className="truncate">{folderLabel(folder)}</span>
                  {folder.sync_error !== null && (
                    <span className="text-danger" aria-label="sync error">
                      {"\u26A0\uFE0F"}
                    </span>
                  )}
                </span>
                {folder.unread > 0 && (
                  <span className="mono shrink-0 rounded-full bg-white/[0.08] px-1.5 text-[11px] text-fg">
                    {folder.unread}
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>
      </nav>
    </aside>
  );
}
