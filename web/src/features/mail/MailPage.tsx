// Lenovmail — authored by satuapps (satuapps.com)
// Mail page `/mail`: three panels (account+folder, message list, reader) for a single active account.
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiError } from "../../api";
import type { MessageQuery } from "../../types";
import { useEventStream } from "../../events";
import FolderSidebar from "./FolderSidebar";
import MessageListPane from "./MessageListPane";
import ReadingPane from "./ReadingPane";
import ComposeModal from "./ComposeModal";
import type { ComposeInitial } from "./ComposeModal";
import { useAccounts, useDebouncedValue, useFolders, useMessageDetail, useMessages } from "./mailHooks";

const NEW_MESSAGE_INITIAL: ComposeInitial = {
  to: "",
  cc: "",
  bcc: "",
  subject: "",
  body: "",
  inReplyTo: null,
  references: [],
};

export default function MailPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { accounts, loading: accountsLoading } = useAccounts();

  const accountParam = searchParams.get("account");
  const folderParam = searchParams.get("folder");
  const messageParam = searchParams.get("message");
  const qParam = searchParams.get("q") ?? "";
  const unread = searchParams.get("unread") === "1";
  const flagged = searchParams.get("flagged") === "1";
  const attachmentsOnly = searchParams.get("attachments") === "1";
  const oauthStatus = searchParams.get("oauth");

  const [searchInput, setSearchInput] = useState(qParam);
  const debouncedSearch = useDebouncedValue(searchInput, 300);
  const [composeInitial, setComposeInitial] = useState<ComposeInitial | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [updateBanner, setUpdateBanner] = useState<string | null>(null);
  const [sentNotice, setSentNotice] = useState(false);

  // Select the first account by default once accounts are available, and persist it in the URL.
  useEffect(() => {
    if (accountParam !== null || accounts.length === 0) return;
    const first = accounts[0];
    if (first === undefined) return;
    const next = new URLSearchParams(searchParams);
    next.set("account", first.id);
    setSearchParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accountParam, accounts]);

  // Sync the local (debounced) search box to the `q` URL parameter.
  useEffect(() => {
    if (debouncedSearch === qParam) return;
    const next = new URLSearchParams(searchParams);
    if (debouncedSearch) next.set("q", debouncedSearch);
    else next.delete("q");
    setSearchParams(next, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedSearch]);

  const activeAccountId = accountParam;
  const activeAccount = accounts.find((account) => account.id === activeAccountId) ?? null;
  const { folders, reload: reloadFolders } = useFolders(activeAccountId);

  const messageQuery: MessageQuery = {
    folder_id: folderParam ?? undefined,
    q: qParam || undefined,
    unread: unread || undefined,
    flagged: flagged || undefined,
    has_attachments: attachmentsOnly || undefined,
  };
  const messagesState = useMessages(activeAccountId, messageQuery);
  const detail = useMessageDetail(messageParam);

  const [oauthBanner, setOauthBanner] = useState<{ status: string } | null>(null);
  useEffect(() => {
    if (oauthStatus === null) return;
    setOauthBanner({ status: oauthStatus });
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete("oauth");
        return next;
      },
      { replace: true },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [oauthStatus]);

  useEventStream((event) => {
    const accountId = typeof event.payload.account_id === "string" ? event.payload.account_id : null;
    if (accountId === null || accountId !== activeAccountId) return;
    if (event.type === "folder.counts") {
      reloadFolders();
    } else if (event.type === "message.new" || event.type === "message.updated") {
      const eventFolderId = typeof event.payload.folder_id === "string" ? event.payload.folder_id : null;
      const matchesFolder = folderParam === null || eventFolderId === null || eventFolderId === folderParam;
      if (matchesFolder) {
        setUpdateBanner(event.type === "message.new" ? "New message." : "Message updated.");
      }
      if (messageParam !== null && (eventFolderId === null || detail.message?.folder_ids.includes(eventFolderId))) {
        detail.reload();
      }
    }
  });

  const updateSearchParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(searchParams);
    if (value === null) next.delete(key);
    else next.set(key, value);
    setSearchParams(next, { replace: key === "message" ? false : true });
  };

  const toggleBoolParam = (key: string, current: boolean) => {
    updateSearchParam(key, current ? null : "1");
  };

  const runAction = (action: () => Promise<void>) => {
    setActionBusy(true);
    setActionError(null);
    void action()
      .catch((err: unknown) => {
        setActionError(err instanceof ApiError ? err.message : "Action failed");
      })
      .finally(() => setActionBusy(false));
  };

  const handleSetFlags = (flags: { seen?: boolean; flagged?: boolean }) => {
    const current = detail.message;
    if (current === null) return;
    runAction(async () => {
      const updated = await api.setFlags(current.id, flags);
      messagesState.patchItem(current.id, updated);
      detail.reload();
    });
  };

  const handleMove = (folderId: string) => {
    const current = detail.message;
    if (current === null) return;
    runAction(async () => {
      await api.moveMessage(current.id, folderId);
      messagesState.removeItem(current.id);
      updateSearchParam("message", null);
      reloadFolders();
    });
  };

  const handleDelete = () => {
    const current = detail.message;
    if (current === null) return;
    if (!window.confirm("Delete this message?")) return;
    runAction(async () => {
      await api.deleteMessage(current.id);
      messagesState.removeItem(current.id);
      updateSearchParam("message", null);
      reloadFolders();
    });
  };

  return (
    <div className="flex h-full min-h-0">
      <FolderSidebar
        accounts={accounts}
        accountsLoading={accountsLoading}
        activeAccountId={activeAccountId}
        onSelectAccount={(id) => {
          const next = new URLSearchParams();
          next.set("account", id);
          setSearchParams(next);
        }}
        folders={folders}
        foldersLoading={false}
        activeFolderId={folderParam}
        onSelectFolder={(id) => updateSearchParam("folder", id)}
      />

      <div className="flex h-full min-h-0 flex-1 flex-col">
        {oauthBanner !== null && (
          <div
            className={`flex items-center justify-between px-4 py-2 text-sm ${
              oauthBanner.status === "ok" ? "bg-ok/10 text-ok" : "bg-danger/10 text-danger"
            }`}
          >
            <span>
              {oauthBanner.status === "ok"
                ? "Microsoft account connected successfully."
                : "Failed to connect Microsoft account."}
            </span>
            <button type="button" onClick={() => setOauthBanner(null)}>
              ×
            </button>
          </div>
        )}
        {sentNotice && (
          <div className="flex items-center justify-between bg-ok/10 px-4 py-2 text-sm text-ok">
            <span>Message queued for sending.</span>
            <div className="flex items-center gap-3">
              <Link to="/outbox" className="underline">
                View Outbox
              </Link>
              <button type="button" onClick={() => setSentNotice(false)}>
                ×
              </button>
            </div>
          </div>
        )}
        {actionError !== null && <div className="bg-danger/10 px-4 py-2 text-sm text-danger">{actionError}</div>}

        <div className="border-b border-ink-600 px-4 py-2">
          <button
            type="button"
            className="btn btn-primary"
            disabled={activeAccountId === null}
            onClick={() => setComposeInitial(NEW_MESSAGE_INITIAL)}
          >
            Compose
          </button>
        </div>

        <div className="flex min-h-0 flex-1">
          <MessageListPane
            items={messagesState.items}
            loading={messagesState.loading}
            loadingMore={messagesState.loadingMore}
            error={messagesState.error}
            hasMore={messagesState.hasMore}
            onLoadMore={messagesState.loadMore}
            activeMessageId={messageParam}
            onSelectMessage={(id) => updateSearchParam("message", id)}
            searchInput={searchInput}
            onSearchInputChange={setSearchInput}
            unreadOnly={unread}
            onToggleUnread={() => toggleBoolParam("unread", unread)}
            flaggedOnly={flagged}
            onToggleFlagged={() => toggleBoolParam("flagged", flagged)}
            attachmentsOnly={attachmentsOnly}
            onToggleAttachments={() => toggleBoolParam("attachments", attachmentsOnly)}
            updateBanner={updateBanner}
            onDismissUpdateBanner={() => setUpdateBanner(null)}
            onRefresh={() => {
              setUpdateBanner(null);
              messagesState.reload();
              reloadFolders();
            }}
          />

          <ReadingPane
            detail={detail}
            folders={folders}
            fromAddress={activeAccount?.email_address ?? ""}
            busy={actionBusy}
            onSetFlags={handleSetFlags}
            onMove={handleMove}
            onDelete={handleDelete}
            onCompose={setComposeInitial}
          />
        </div>
      </div>

      {composeInitial !== null && activeAccountId !== null && (
        <ComposeModal
          accountId={activeAccountId}
          fromAddress={activeAccount?.email_address ?? ""}
          initial={composeInitial}
          onClose={() => setComposeInitial(null)}
          onSent={() => {
            setComposeInitial(null);
            setSentNotice(true);
          }}
        />
      )}
    </div>
  );
}
