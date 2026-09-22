// Lenovmail — authored by satuapps (satuapps.com)
// Compose modal: new message, reply, reply all, or forward.
import { useState } from "react";
import { api, ApiError } from "../../api";
import type { OutboxAttachment } from "../../types";
import { base64FromDataUrl, parseAddressList } from "./mailUtils";

export interface ComposeInitial {
  to: string;
  cc: string;
  bcc: string;
  subject: string;
  body: string;
  inReplyTo: string | null;
  references: string[];
}

interface ComposeModalProps {
  accountId: string;
  fromAddress: string;
  initial: ComposeInitial;
  onClose: () => void;
  onSent: () => void;
}

async function fileToAttachment(file: File): Promise<OutboxAttachment> {
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error ?? new Error("Failed to read file"));
    reader.readAsDataURL(file);
  });
  return {
    filename: file.name,
    mime_type: file.type || "application/octet-stream",
    content_b64: base64FromDataUrl(dataUrl),
  };
}

export default function ComposeModal({ accountId, fromAddress, initial, onClose, onSent }: ComposeModalProps) {
  const [to, setTo] = useState(initial.to);
  const [cc, setCc] = useState(initial.cc);
  const [bcc, setBcc] = useState(initial.bcc);
  const [subject, setSubject] = useState(initial.subject);
  const [body, setBody] = useState(initial.body);
  const [files, setFiles] = useState<File[]>([]);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSend = () => {
    const toList = parseAddressList(to);
    if (toList.length === 0) {
      setError("Enter at least one recipient address.");
      return;
    }
    setSending(true);
    setError(null);
    void (async () => {
      try {
        const attachments = await Promise.all(files.map(fileToAttachment));
        await api.queueMessage(
          accountId,
          {
            from: fromAddress,
            to: toList,
            cc: parseAddressList(cc),
            bcc: parseAddressList(bcc),
            subject,
            text: body,
            in_reply_to: initial.inReplyTo,
            references: initial.references,
            attachments,
          },
          false,
        );
        onSent();
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "Failed to send message");
      } finally {
        setSending(false);
      }
    })();
  };

  return (
    <div className="fixed inset-0 z-20 flex items-center justify-center bg-ink-900/70 p-4">
      <div className="flex max-h-full w-full max-w-2xl flex-col rounded-lg border border-ink-600 bg-ink-800">
        <div className="flex items-center justify-between border-b border-ink-600 px-4 py-2">
          <h2 className="text-sm font-semibold text-fg">Compose</h2>
          <button type="button" className="btn btn-ghost" onClick={onClose} disabled={sending}>
            Close
          </button>
        </div>
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
          <div>
            <label className="label" htmlFor="compose-to">
              To
            </label>
            <input
              id="compose-to"
              className="input"
              value={to}
              onChange={(event) => setTo(event.target.value)}
              placeholder="name@example.com, name2@example.com"
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="label" htmlFor="compose-cc">
                Cc
              </label>
              <input id="compose-cc" className="input" value={cc} onChange={(event) => setCc(event.target.value)} />
            </div>
            <div>
              <label className="label" htmlFor="compose-bcc">
                Bcc
              </label>
              <input
                id="compose-bcc"
                className="input"
                value={bcc}
                onChange={(event) => setBcc(event.target.value)}
              />
            </div>
          </div>
          <div>
            <label className="label" htmlFor="compose-subject">
              Subject
            </label>
            <input
              id="compose-subject"
              className="input"
              value={subject}
              onChange={(event) => setSubject(event.target.value)}
            />
          </div>
          <div>
            <label className="label" htmlFor="compose-body">
              Message
            </label>
            <textarea
              id="compose-body"
              className="input min-h-[220px] resize-y"
              value={body}
              onChange={(event) => setBody(event.target.value)}
            />
          </div>
          <div>
            <label className="label" htmlFor="compose-files">
              Attachments
            </label>
            <input
              id="compose-files"
              type="file"
              multiple
              className="text-sm text-fg-muted"
              onChange={(event) => setFiles(event.target.files ? Array.from(event.target.files) : [])}
            />
            {files.length > 0 && (
              <ul className="mt-1 space-y-0.5 text-xs text-fg-muted">
                {files.map((file) => (
                  <li key={file.name}>{file.name}</li>
                ))}
              </ul>
            )}
          </div>
          {error !== null && <p className="text-sm text-danger">{error}</p>}
        </div>
        <div className="flex justify-end gap-2 border-t border-ink-600 px-4 py-2">
          <button type="button" className="btn btn-ghost" onClick={onClose} disabled={sending}>
            Cancel
          </button>
          <button type="button" className="btn btn-primary" onClick={handleSend} disabled={sending}>
            {sending ? "Sending…" : "Send"}
          </button>
        </div>
      </div>
    </div>
  );
}
