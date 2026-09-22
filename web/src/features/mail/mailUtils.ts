// Lenovmail — authored by satuapps (satuapps.com)
// Pure utilities for the mail page: no React state, easy to test in isolation.
import type { Attachment, Folder } from "../../types";

/** Split input like "a@x.com, b@y.com" into a clean list of addresses. */
export function parseAddressList(raw: string): string[] {
  return raw
    .split(",")
    .map((part) => part.trim())
    .filter((part) => part.length > 0);
}

/**
 * Replace `cid:...` references in `src`/`href` with the matching inline attachment download URL.
 * Runs after DOMPurify sanitization so it only manipulates already-safe attribute values.
 */
export function resolveInlineCids(
  html: string,
  messageId: string,
  attachments: Attachment[],
  attachmentUrl: (messageId: string, attachmentId: string) => string,
): string {
  let result = html;
  for (const att of attachments) {
    if (!att.is_inline || !att.content_id) continue;
    const cid = att.content_id
      .replace(/^<|>$/g, "")
      .replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const url = attachmentUrl(messageId, att.id);
    const pattern = new RegExp(`(src|href)=("|')cid:${cid}\\2`, "gi");
    result = result.replace(
      pattern,
      (_match, attr: string, quote: string) => `${attr}=${quote}${url}${quote}`,
    );
  }
  return result;
}

/** Wrap sanitized message HTML into a full document with a CSP `img-src`. */
export function buildReadingSrcDoc(
  bodyHtml: string,
  allowRemoteImages: boolean,
): string {
  const csp = allowRemoteImages
    ? "img-src data: https: http: 'self';"
    : "img-src data: 'self';";
  return (
    `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${csp}">` +
    `<style>body{font-family:ui-sans-serif,system-ui,-apple-system,sans-serif;color:#0b0f14;background:#fff;` +
    `margin:0;padding:12px;word-wrap:break-word;} img{max-width:100%;} a{color:#3d84f7;}</style></head>` +
    `<body>${bodyHtml}</body></html>`
  );
}

/** Read the `Message-ID:` header from a raw RFC822 source (used for In-Reply-To/References). */
export function extractMessageIdHeader(rawSource: string): string | null {
  const match = /^message-id:\s*(<[^>]+>)/im.exec(rawSource);
  return match ? (match[1] ?? null) : null;
}

const FOLDER_ROLE_ICON: Record<string, string> = {
  inbox: "\u{1F4E5}",
  sent: "\u{1F4E4}",
  drafts: "\u{1F4DD}",
  trash: "\u{1F5D1}",
  junk: "\u26A0\uFE0F",
  archive: "\u{1F4E6}",
  other: "\u{1F4C1}",
};

export function folderIcon(role: string): string {
  return FOLDER_ROLE_ICON[role] ?? FOLDER_ROLE_ICON.other ?? "\u{1F4C1}";
}

const FOLDER_ROLE_LABEL: Record<string, string> = {
  inbox: "Inbox",
  sent: "Sent",
  drafts: "Drafts",
  trash: "Trash",
  junk: "Junk",
  archive: "Archive",
  other: "Other",
};

export function folderLabel(folder: Folder): string {
  return folder.name || FOLDER_ROLE_LABEL[folder.role] || folder.role;
}

/** Color of the folder sync-state dot. */
export function syncStateDotClass(state: string): string {
  if (state === "syncing") return "bg-accent";
  if (state === "error") return "bg-danger";
  return "bg-ok";
}

/**
 * `error` and `auth_error` are different problems: `error` means the server could not be
 * reached and the next sync may well succeed, `auth_error` means the server rejected the
 * credentials and a human has to re-authenticate. They get different colors accordingly.
 */
export function accountStatusChipClass(status: string): string {
  if (status === "active") return "chip chip-ok";
  if (status === "auth_error") return "chip chip-danger";
  if (status === "error") return "chip chip-warn";
  return "chip";
}

export const ACCOUNT_STATUS_LABEL: Record<string, string> = {
  active: "active",
  auth_error: "auth reject",
  error: "unreachable",
  disabled: "disconnected",
};

/** Sentinel account id for the "All accounts" scope in the mail page URL. */
export const ALL_ACCOUNTS = "all";

/** Whole days from now until `iso`, floored at 0; `null` when there is no date. */
export function daysUntil(iso: string | null): number | null {
  if (!iso) return null;
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return null;
  return Math.max(0, Math.ceil((at - Date.now()) / 86_400_000));
}

export function outboxStatusChipClass(status: string): string {
  switch (status) {
    case "sent":
      return "chip chip-ok";
    case "queued":
      return "chip chip-warn";
    case "sending":
      return "chip border-accent/40 text-accent";
    case "pending_approval":
    case "failed":
      return "chip chip-danger";
    default:
      return "chip";
  }
}

export const OUTBOX_STATUS_LABEL: Record<string, string> = {
  queued: "Queued",
  pending_approval: "Needs Approval",
  sending: "Sending",
  sent: "Sent",
  failed: "Failed",
};

/** Strip repeated "Re:"/"Fwd:" prefixes (including localized variants) before adding a new one.
 *  Keep in sync with `_REPLY_PREFIX` in src/lenovmail/sync/normalize.py. */
export function stripSubjectPrefix(subject: string): string {
  return subject
    .replace(/^((re|fw|fwd|aw|sv|vs|antw|bls|balas)\s*(\[\d+\])?:\s*)+/i, "")
    .trim();
}

export function base64FromDataUrl(dataUrl: string): string {
  const commaIndex = dataUrl.indexOf(",");
  return commaIndex === -1 ? dataUrl : dataUrl.slice(commaIndex + 1);
}
