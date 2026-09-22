// Lenovmail — authored by satuapps
// Types mirrored from `src/lenovmail/api/schemas.py`. Don't add ad-hoc types in
// features: if the backend changes, change it here so every usage is caught.
export type UUID = string;

export interface User {
  id: UUID;
  email: string;
  role: string;
  is_active: boolean;
  created_at: string;
}

export interface Health {
  status: string;
  database: boolean;
  redis: boolean;
  version: string;
}

export interface ServerIn {
  host: string;
  port: number;
  security: "ssl" | "starttls" | "none";
  username?: string | null;
  password?: string | null;
}

export interface ServerOut {
  host: string;
  port: number;
  security: string;
  auth: string;
  username: string | null;
}

export interface DiscoverResult {
  source: string;
  provider: string;
  oauth_required: boolean;
  imap: ServerOut | null;
  smtp: ServerOut | null;
  warnings: string[];
}

export interface Account {
  id: UUID;
  email_address: string;
  display_name: string | null;
  provider: string;
  status: string;
  status_detail: string | null;
  sync_interval_s: number;
  last_sync_at: string | null;
  unread: number;
  total: number;
  oauth_url: string | null;
}

export interface AccountCreate {
  email_address: string;
  display_name?: string | null;
  provider?: "auto" | "imap" | "graph";
  password?: string | null;
  imap?: ServerIn | null;
  smtp?: ServerIn | null;
  sync_interval_s?: number | null;
}

export interface AccountUpdate {
  display_name?: string | null;
  sync_interval_s?: number | null;
  status?: "active" | "disabled" | null;
  password?: string | null;
}

export interface AccountTest {
  imap: string | null;
  smtp: string | null;
  graph: string | null;
  folders: number | null;
}

export interface Folder {
  id: UUID;
  remote_id: string;
  name: string;
  role: string;
  parent_id: UUID | null;
  sync_state: string;
  sync_error: string | null;
  last_synced_at: string | null;
  unread: number;
  total: number;
}

export interface Address {
  name: string | null;
  addr: string;
}

export interface Message {
  id: UUID;
  account_id: UUID;
  thread_id: UUID | null;
  subject: string | null;
  from_name: string | null;
  from_addr: string | null;
  to: Address[];
  snippet: string | null;
  internal_date: string | null;
  sent_date: string | null;
  has_attachments: boolean;
  body_state: string;
  size_bytes: number | null;
  seen: boolean;
  flagged: boolean;
  folder_ids: UUID[];
}

export interface MessagePage {
  items: Message[];
  next_cursor: string | null;
}

export interface Attachment {
  id: UUID;
  filename: string | null;
  mime_type: string | null;
  size_bytes: number | null;
  is_inline: boolean;
  content_id: string | null;
}

export interface MessageBody {
  body_state: string;
  text: string | null;
  html: string | null;
  attachments: Attachment[];
}

export interface MessageQuery {
  folder_id?: UUID;
  q?: string;
  unread?: boolean;
  flagged?: boolean;
  has_attachments?: boolean;
  thread_id?: UUID;
  limit?: number;
  cursor?: string | null;
}

export interface Thread {
  thread_id: UUID;
  subject: string | null;
  message_count: number;
  items: Message[];
}

export interface OutboxAttachment {
  filename: string;
  mime_type: string;
  content_b64: string;
}

export interface OutboxPayload {
  from: string;
  to: string[];
  cc?: string[];
  bcc?: string[];
  subject?: string;
  text?: string | null;
  html?: string | null;
  in_reply_to?: string | null;
  references?: string[];
  attachments?: OutboxAttachment[];
}

export interface OutboxItem {
  id: UUID;
  account_id: UUID;
  status: string;
  payload: OutboxPayload & Record<string, unknown>;
  attempts: number;
  last_error: string | null;
  created_at: string;
  sent_at: string | null;
}

export interface AgentToken {
  id: UUID;
  name: string;
  scopes: string[];
  account_ids: UUID[] | null;
  require_send_approval: boolean;
  send_limit_per_hour: number;
  expires_at: string | null;
  last_used_at: string | null;
  revoked_at: string | null;
  created_at: string;
}

export interface AgentTokenCreated extends AgentToken {
  token: string;
}

export interface AgentTokenCreate {
  name: string;
  scopes?: string[];
  account_ids?: UUID[] | null;
  require_send_approval?: boolean;
  send_limit_per_hour?: number | null;
  expires_in_days?: number | null;
}

export interface AgentAuditEntry {
  id: number;
  token_id: UUID | null;
  user_id: UUID | null;
  tool: string;
  outcome: string;
  error: string | null;
  target_ids: unknown[] | null;
  created_at: string;
}

/** SSE event from `GET /api/events` (see `sync/events.py`). */
export type EventType =
  | "message.new"
  | "message.updated"
  | "folder.counts"
  | "account.status"
  | "sync.progress";

export interface ServerEvent {
  type: EventType;
  payload: Record<string, unknown>;
}

export const AGENT_SCOPES = [
  "mail.read",
  "mail.write",
  "mail.send",
  "mail.delete",
  "mail.manage",
] as const;
