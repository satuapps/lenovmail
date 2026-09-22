// Lenovmail — authored by satuapps (satuapps.com)
// MailPage right panel: header, action toolbar, message body (sanitized HTML / plain text), and attachments.
import { useMemo, useState } from "react";
import DOMPurify from "dompurify";
import { api } from "../../api";
import type { Attachment, Folder, Message } from "../../types";
import { formatBytes, fullDate, senderLabel } from "../../format";
import { buildReadingSrcDoc, extractMessageIdHeader, resolveInlineCids, stripSubjectPrefix } from "./mailUtils";
import type { ComposeInitial } from "./ComposeModal";

interface MessageDetail {
  message: Message | null;
  bodyText: string | null;
  bodyHtml: string | null;
  bodyState: string | null;
  attachments: Attachment[];
  loading: boolean;
  error: string | null;
  polling: boolean;
  startPolling: () => void;
  stopPolling: () => void;
}

interface ReadingPaneProps {
  detail: MessageDetail;
  folders: Folder[];
  fromAddress: string;
  busy: boolean;
  onSetFlags: (flags: { seen?: boolean; flagged?: boolean }) => void;
  onMove: (folderId: string) => void;
  onDelete: () => void;
  onCompose: (initial: ComposeInitial) => void;
}

async function fetchOriginalMessageId(messageId: string): Promise<string | null> {
  const response = await fetch(api.rawUrl(messageId), { credentials: "include" });
  if (!response.ok) return null;
  const text = await response.text();
  return extractMessageIdHeader(text);
}

function quoteText(prefixLine: string, body: string | null): string {
  const quoted = (body ?? "").split("\n").map((line) => `> ${line}`).join("\n");
  return `\n\n${prefixLine}\n${quoted}`;
}

export default function ReadingPane({
  detail,
  folders,
  fromAddress,
  busy,
  onSetFlags,
  onMove,
  onDelete,
  onCompose,
}: ReadingPaneProps) {
  const [allowRemoteImages, setAllowRemoteImages] = useState(false);
  const [composeError, setComposeError] = useState<string | null>(null);
  const { message, bodyText, bodyHtml, bodyState, attachments, loading, error, polling, startPolling, stopPolling } =
    detail;

  const srcDoc = useMemo(() => {
    if (bodyHtml === null || message === null) return null;
    const sanitized = DOMPurify.sanitize(bodyHtml, {
      FORBID_TAGS: ["script", "style", "form", "iframe", "object", "embed"],
      FORBID_ATTR: ["onerror", "onload", "onclick"],
    });
    const resolved = resolveInlineCids(sanitized, message.id, attachments, api.attachmentUrl);
    return buildReadingSrcDoc(resolved, allowRemoteImages);
  }, [bodyHtml, message, attachments, allowRemoteImages]);

  if (message === null) {
    return (
      <section className="flex h-full min-h-0 flex-1 items-center justify-center text-sm text-fg-dim">
        {loading ? "Loading message…" : "Select a message to read."}
      </section>
    );
  }

  const startCompose = (mode: "reply" | "replyAll" | "forward") => {
    setComposeError(null);
    void (async () => {
      let inReplyTo: string | null = null;
      try {
        inReplyTo = await fetchOriginalMessageId(message.id);
      } catch {
        // Message-ID header is optional for threading; sending can still proceed without it.
      }
      const references = inReplyTo !== null ? [inReplyTo] : [];
      const strippedSubject = stripSubjectPrefix(message.subject ?? "");
      if (mode === "forward") {
        onCompose({
          to: "",
          cc: "",
          bcc: "",
          subject: `Fwd: ${strippedSubject}`,
          body: quoteText(
            `--- Forwarded message from ${senderLabel(message.from_name, message.from_addr)} (${fullDate(message.sent_date)}) ---`,
            bodyText,
          ).trimStart(),
          inReplyTo,
          references,
        });
        return;
      }
      const to = message.from_addr ? [message.from_addr] : [];
      if (mode === "replyAll") {
        for (const recipient of message.to) {
          if (recipient.addr && recipient.addr.toLowerCase() !== fromAddress.toLowerCase() && !to.includes(recipient.addr)) {
            to.push(recipient.addr);
          }
        }
      }
      onCompose({
        to: to.join(", "),
        cc: "",
        bcc: "",
        subject: `Re: ${strippedSubject}`,
        body: quoteText(
          `On ${fullDate(message.sent_date)}, ${senderLabel(message.from_name, message.from_addr)} wrote:`,
          bodyText,
        ),
        inReplyTo,
        references,
      });
    })().catch(() => setComposeError("Failed to fetch Message-ID header for the reply."));
  };

  const otherFolders = folders.filter((folder) => !message.folder_ids.includes(folder.id));

  return (
    <section className="flex h-full min-h-0 flex-1 flex-col">
      <header className="space-y-2 border-b border-ink-600 p-4">
        <div className="flex items-start justify-between gap-3">
          <h1 className="text-lg font-semibold text-fg">{message.subject || "(no subject)"}</h1>
          <span className="chip shrink-0">{bodyState ?? message.body_state}</span>
        </div>
        <div className="text-sm text-fg-muted">
          From <span className="text-fg">{senderLabel(message.from_name, message.from_addr)}</span>
          {message.from_addr ? ` <${message.from_addr}>` : ""}
        </div>
        <div className="text-xs text-fg-dim">
          To {message.to.map((addr) => addr.name ? `${addr.name} <${addr.addr}>` : addr.addr).join(", ") || "—"}
        </div>
        <div className="text-xs text-fg-dim">{fullDate(message.sent_date)}</div>
      </header>

      <div className="flex flex-wrap items-center gap-1.5 border-b border-ink-600 px-4 py-2">
        <button
          type="button"
          className="btn"
          disabled={busy}
          onClick={() => onSetFlags({ seen: !message.seen })}
        >
          {message.seen ? "Mark as unread" : "Mark as read"}
        </button>
        <button
          type="button"
          className="btn"
          disabled={busy}
          onClick={() => onSetFlags({ flagged: !message.flagged })}
        >
          {message.flagged ? "Remove flag" : "Add flag"}
        </button>
        <select
          className="input w-auto"
          disabled={busy || otherFolders.length === 0}
          value=""
          onChange={(event) => {
            if (event.target.value) onMove(event.target.value);
          }}
        >
          <option value="">Move to…</option>
          {otherFolders.map((folder) => (
            <option key={folder.id} value={folder.id}>
              {folder.name}
            </option>
          ))}
        </select>
        <button type="button" className="btn btn-danger" disabled={busy} onClick={onDelete}>
          Delete
        </button>
        <a className="btn btn-ghost" href={api.rawUrl(message.id)} target="_blank" rel="noreferrer">
          View Raw
        </a>
        <span className="ml-auto flex items-center gap-1.5">
          <button type="button" className="btn btn-primary" onClick={() => startCompose("reply")}>
            Reply
          </button>
          <button type="button" className="btn" onClick={() => startCompose("replyAll")}>
            Reply All
          </button>
          <button type="button" className="btn" onClick={() => startCompose("forward")}>
            Forward
          </button>
        </span>
      </div>

      {composeError !== null && <p className="px-4 pt-2 text-xs text-danger">{composeError}</p>}

      <div className="min-h-0 flex-1 overflow-y-auto">
        {error !== null && <p className="p-4 text-sm text-danger">{error}</p>}

        {bodyState !== "full" && (
          <div className="flex items-center gap-2 border-b border-ink-600 bg-ink-800 px-4 py-2 text-xs text-fg-muted">
            <span>Message body incomplete ({bodyState ?? "none"}).</span>
            {!polling ? (
              <button type="button" className="btn btn-ghost" onClick={startPolling}>
                Load content
              </button>
            ) : (
              <>
                <span>Loading…</span>
                <button type="button" className="btn btn-ghost" onClick={stopPolling}>
                  Stop
                </button>
              </>
            )}
          </div>
        )}

        {srcDoc !== null && (
          <div className="border-b border-ink-600 px-4 py-1.5">
            <button type="button" className="text-xs text-accent underline" onClick={() => setAllowRemoteImages((v) => !v)}>
              {allowRemoteImages ? "Hide remote images" : "Show images"}
            </button>
          </div>
        )}

        {srcDoc !== null ? (
          <iframe
            title="Message content"
            sandbox="allow-popups allow-popups-to-escape-sandbox"
            srcDoc={srcDoc}
            className="h-full min-h-[300px] w-full bg-white"
          />
        ) : bodyText !== null ? (
          <pre className="whitespace-pre-wrap p-4 text-sm text-fg">{bodyText}</pre>
        ) : (
          <p className="p-4 text-sm text-fg-dim">No content to display.</p>
        )}

        {attachments.length > 0 && (
          <div className="border-t border-ink-600 p-4">
            <h2 className="label">Attachments</h2>
            <ul className="mt-1 space-y-1">
              {attachments.map((att) => (
                <li key={att.id} className="flex items-center justify-between text-sm">
                  <span className="truncate text-fg">{att.filename || "(no filename)"}</span>
                  <span className="ml-2 flex items-center gap-2 text-xs text-fg-dim">
                    {formatBytes(att.size_bytes)}
                    <a
                      className="text-accent underline"
                      href={api.attachmentUrl(message.id, att.id)}
                      download={att.filename ?? undefined}
                    >
                      Download
                    </a>
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </section>
  );
}
