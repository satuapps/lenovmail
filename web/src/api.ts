// Lenovmail — authored by satuapps (satuapps.com)
import type {
  Account,
  AccountCreate,
  AccountTest,
  AccountUpdate,
  AgentAuditEntry,
  AgentToken,
  AgentTokenCreate,
  AgentTokenCreated,
  AuditQuery,
  DiscoverResult,
  Folder,
  GlobalMessageQuery,
  Health,
  Message,
  MessageBody,
  MessagePage,
  MessageQuery,
  MessageValue,
  OpsSummary,
  OutboxItem,
  OutboxPayload,
  Overview,
  SessionInfo,
  Thread,
  User,
} from "./types";

/** HTTP error from the API; `detail` is preserved so it can be displayed as-is. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function messageFromDetail(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    // FastAPI validation error: [{loc, msg, type}, ...]
    const parts = detail
      .map((item) =>
        typeof item === "object" && item !== null && "msg" in item
          ? String((item as { msg: unknown }).msg)
          : String(item),
      )
      .filter(Boolean);
    if (parts.length) return parts.join("; ");
  }
  return fallback;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, {
    ...init,
    headers,
    credentials: "include",
  });
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  if (!response.ok) {
    let detail: unknown;
    try {
      detail = text
        ? (JSON.parse(text) as { detail?: unknown }).detail
        : undefined;
    } catch {
      detail = text;
    }
    throw new ApiError(
      response.status,
      messageFromDetail(
        detail,
        `HTTP ${response.status} ${response.statusText}`,
      ),
    );
  }
  return (text ? JSON.parse(text) : undefined) as T;
}

type QueryValue =
  string | number | boolean | undefined | null | readonly string[];

/** Repeats a key per element for array values, which is what FastAPI's `Query(list)` expects. */
function queryString(params: Record<string, QueryValue>) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      for (const item of value) search.append(key, String(item));
      continue;
    }
    search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

export const api = {
  // --- auth -------------------------------------------------------------
  login: (email: string, password: string) =>
    request<User>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  logout: () => request<void>("/api/auth/logout", { method: "POST" }),
  me: () => request<User>("/api/auth/me"),
  changePassword: (current_password: string, new_password: string) =>
    request<void>("/api/auth/password", {
      method: "POST",
      body: JSON.stringify({ current_password, new_password }),
    }),
  health: () => request<Health>("/api/healthz"),

  // --- accounts -----------------------------------------------------------
  discover: (email: string, password?: string) =>
    request<DiscoverResult>("/api/accounts/discover", {
      method: "POST",
      body: JSON.stringify({ email, password: password || null }),
    }),
  listAccounts: () => request<Account[]>("/api/accounts"),
  getAccount: (id: string) => request<Account>(`/api/accounts/${id}`),
  createAccount: (body: AccountCreate) =>
    request<Account>("/api/accounts", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateAccount: (id: string, body: AccountUpdate) =>
    request<Account>(`/api/accounts/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteAccount: (id: string) =>
    request<void>(`/api/accounts/${id}`, { method: "DELETE" }),
  syncAccount: (id: string) =>
    request<{ job_id?: string; status?: string }>(`/api/accounts/${id}/sync`, {
      method: "POST",
    }),
  testAccount: (id: string) =>
    request<AccountTest>(`/api/accounts/${id}/test`, { method: "POST" }),

  // --- folders & messages -------------------------------------------------
  listFolders: (accountId: string, limit = 500) =>
    request<Folder[]>(
      `/api/accounts/${accountId}/folders${queryString({ limit })}`,
    ),
  listMessages: (accountId: string, query: MessageQuery = {}) =>
    request<MessagePage>(
      `/api/accounts/${accountId}/messages${queryString({ ...query })}`,
    ),
  /** Cross-account search; omit `account_id` to cover every readable account. */
  searchMessages: (query: GlobalMessageQuery = {}) =>
    request<MessagePage>(`/api/messages${queryString({ ...query })}`),
  getMessage: (id: string) => request<Message>(`/api/messages/${id}`),
  getBody: (id: string) => request<MessageBody>(`/api/messages/${id}/body`),
  rawUrl: (id: string) => `/api/messages/${id}/raw`,
  attachmentUrl: (messageId: string, attachmentId: string) =>
    `/api/messages/${messageId}/attachments/${attachmentId}`,
  setFlags: (id: string, flags: { seen?: boolean; flagged?: boolean }) =>
    request<Message>(`/api/messages/${id}/flags`, {
      method: "POST",
      body: JSON.stringify(flags),
    }),
  moveMessage: (id: string, folderId: string) =>
    request<void>(`/api/messages/${id}/move`, {
      method: "POST",
      body: JSON.stringify({ folder_id: folderId }),
    }),
  deleteMessage: (id: string) =>
    request<void>(`/api/messages/${id}`, { method: "DELETE" }),
  getThread: (threadId: string) => request<Thread>(`/api/threads/${threadId}`),

  messageValues: (id: string, reveal = false) =>
    request<MessageValue[]>(
      `/api/messages/${id}/values${queryString({ reveal })}`,
    ),

  // --- outbox -----------------------------------------------------------
  queueMessage: (
    accountId: string,
    payload: OutboxPayload,
    requiresApproval: boolean,
  ) =>
    request<OutboxItem>("/api/accounts/" + accountId + "/outbox", {
      method: "POST",
      body: JSON.stringify({
        account_id: accountId,
        payload,
        requires_approval: requiresApproval,
      }),
    }),
  listOutbox: (accountId: string, limit = 50) =>
    request<OutboxItem[]>(
      `/api/accounts/${accountId}/outbox${queryString({ limit })}`,
    ),
  approveOutbox: (outboxId: string) =>
    request<OutboxItem>(`/api/outbox/${outboxId}/approve`, { method: "POST" }),

  // --- agent tokens ---------------------------------------------------------
  listTokens: () => request<AgentToken[]>("/api/agent/tokens"),
  createToken: (body: AgentTokenCreate) =>
    request<AgentTokenCreated>("/api/agent/tokens", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  revokeToken: (id: string) =>
    request<void>(`/api/agent/tokens/${id}`, { method: "DELETE" }),
  listAudit: (query: AuditQuery = {}) =>
    request<AgentAuditEntry[]>(
      `/api/agent/audit${queryString({ limit: 200, ...query })}`,
    ),

  // --- sessions -------------------------------------------------------------
  listSessions: () => request<SessionInfo[]>("/api/auth/sessions"),
  revokeSession: (id: string) =>
    request<void>(`/api/auth/sessions/${id}`, { method: "DELETE" }),

  // --- dashboards -----------------------------------------------------------
  statsOverview: () => request<Overview>("/api/stats/overview"),
  opsSummary: () => request<OpsSummary>("/api/ops/summary"),
};
