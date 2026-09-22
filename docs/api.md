# REST API

Base path: `/api`. Full interactive reference — generated from the same FastAPI app,
always in sync with the code — is served live at `GET /api/docs`
(OpenAPI schema at `/api/openapi.json`).

## Auth

Two caller kinds, handled by one dependency (`current_principal`,
`src/lenovmail/api/deps.py`):

| Kind | Credential | Scope |
|---|---|---|
| Browser | `ln_session` HttpOnly cookie, set by `POST /api/auth/login` | all five scopes implicitly |
| AI agent | `Authorization: Bearer lnv_...` | exactly the scopes on the `agent_tokens` row |

A request with neither gets `401`. An agent token that is revoked, expired, or whose
owner is inactive also gets `401`.

The five scopes (`mail.read`, `mail.write`, `mail.send`, `mail.delete`, `mail.manage`) are
documented in `docs/agents.md`. Mutating routes call `principal.require(scope)` directly;
routes that return mailbox content declare `Depends(require_read)` and need `mail.read`.
Account ownership and the token's account allowlist are checked on top of the scope, so a
token scoped to one account gets `403` on every other one.

## Error body

Every error response (validation, `HTTPException`, or the unhandled-exception handler)
has the same shape:

```json
{ "detail": "human-readable message" }
```

`detail` is a string for handwritten errors and a structured list of
`{"loc", "msg", "type"}` objects for FastAPI's own request-validation (422) failures.

## Auth / session

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/api/auth/login` | none | Email + password login; sets the session cookie. Rate-limited to 10 failed attempts per email per 15 minutes. |
| POST | `/api/auth/logout` | session or token | Deletes the Redis session and clears the cookie. |
| GET | `/api/auth/me` | session or token | Returns the caller's `UserOut`. |
| POST | `/api/auth/password` | session or token | Changes the password; revokes every session for the user, including the caller's, and issues it a new one. |

## Accounts, discovery, OAuth

| Method | Path | Auth | Query/body | Description |
|---|---|---|---|---|
| POST | `/api/accounts/discover` | `mail.manage` | body: `email`, `password?` | Runs the ISPDB/SRV/MX/probe discovery chain without persisting anything. |
| GET | `/api/accounts` | `mail.read` | — | Lists accounts owned by the caller, with unread/total counts. |
| POST | `/api/accounts` | `mail.manage` | body: `email_address`, `display_name?`, `provider="auto"\|"imap"\|"graph"`, `password?`, `imap?`, `smtp?`, `sync_interval_s?` | Creates an account. IMAP is auto-discovered and login-verified before it's considered active; `graph` returns `oauth_url` to complete Microsoft consent. |
| GET | `/api/accounts/{account_id}` | `mail.read` | — | One account. |
| PATCH | `/api/accounts/{account_id}` | `mail.manage` | body: `display_name?`, `sync_interval_s?`, `status?`, `password?` | Updates account settings. |
| DELETE | `/api/accounts/{account_id}` | `mail.manage` | — | Deletes the account (cascades to its mail) and resets its connection pool. `204`. |
| POST | `/api/accounts/{account_id}/sync` | `mail.read` | — | Enqueues one `sync_account` job. `202`, returns `{"job_id"}`. |
| POST | `/api/accounts/{account_id}/test` | `mail.read` | — | Tests IMAP/Graph and SMTP connectivity without changing data; returns per-channel status strings. |
| GET | `/api/oauth/microsoft/start` | `mail.manage` | query: `account_id` | Redirects to the Microsoft consent page for an existing Graph account. |
| GET | `/api/oauth/microsoft/callback` | none (state-verified) | query: `code`, `state` | OAuth redirect target; stores the token cache and redirects back into the GUI. |

## Folders

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/accounts/{account_id}/folders` | `mail.read` | Folders with per-folder unread/total counts and sync state. |

## Messages

| Method | Path | Auth | Query/body | Description |
|---|---|---|---|---|
| GET | `/api/accounts/{account_id}/messages` | `mail.read` | `folder_id?`, `q?` (full-text), `unread?`, `flagged?`, `has_attachments?`, `thread_id?`, `limit=50` (1-200), `cursor?` | Keyset-paginated message list, newest first. |
| GET | `/api/messages/{message_id}` | `mail.read` | — | One message's metadata and flags. |
| GET | `/api/messages/{message_id}/body` | `mail.read` | — | Text/HTML body plus attachment metadata (`BodyOut`). |
| GET | `/api/messages/{message_id}/raw` | `mail.read` | — | Raw RFC822 bytes from the blob store, as `message/rfc822`. |
| GET | `/api/messages/{message_id}/attachments/{attachment_id}` | `mail.read` | — | Downloads one attachment (`Content-Disposition: attachment`). |
| POST | `/api/messages/{message_id}/flags` | `mail.write` | body: `seen?`, `flagged?` (at least one) | Updates read/flagged status. |
| POST | `/api/messages/{message_id}/move` | `mail.write` | body: `folder_id` | Moves the message to another folder in the same account. `202`. |
| DELETE | `/api/messages/{message_id}` | `mail.delete` | — | Deletes the message (moved to Trash where the server supports it). `202`. |

### Cursor pagination

`next_cursor` is a keyset over `(internal_date, id)` — never `OFFSET` — so later pages
don't get slower. It is an opaque, base64url-encoded, version-tagged string
(`sync/queries.py:encode_cursor`/`decode_cursor`); treat it as an opaque token, pass it
back verbatim as `cursor` to fetch the next page, and stop paging once `next_cursor` is
`null`. An unparseable cursor returns `400 invalid cursor`; a `folder_id` that doesn't
belong to the account returns `404`.

## Threads

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/threads/{thread_id}` | `mail.read` | Every message in the conversation, ordered by time (`ThreadOut`). |

## Outbox and approval

| Method | Path | Auth | Query/body | Description |
|---|---|---|---|---|
| POST | `/api/accounts/{account_id}/outbox` | `mail.send` | body: `payload` (`from`, `to`, `cc?`, `bcc?`, `subject?`, `text?`, `html?`, `in_reply_to?`, `references?`, `attachments?`), `requires_approval=false` | Queues an outgoing message. For agent tokens, the send limit is enforced and `requires_approval` is OR'd with the token's `require_send_approval`. Status is `pending_approval` or `queued`. `201`. |
| GET | `/api/accounts/{account_id}/outbox` | `mail.read` | `limit=50` (1-200) | Lists outbox rows for the account, newest first. |
| POST | `/api/outbox/{outbox_id}/approve` | `mail.send` | — | Moves a `pending_approval` row to `queued`; the next delivery pass picks it up. |

Send-limit rejections return `429 Too Many Requests` with `detail` naming the used/limit
counts (see `docs/agents.md#send-limit`).

## Agent tokens and audit

| Method | Path | Auth | Query/body | Description |
|---|---|---|---|---|
| GET | `/api/agent/tokens` | session or token | — | Lists the caller's agent tokens (never the plaintext value). |
| POST | `/api/agent/tokens` | session or token | body: `name`, `scopes` (≥1, from the five known scopes), `account_ids?`, `require_send_approval=true`, `send_limit_per_hour?` (default from `LENOVMAIL_AGENT_SEND_LIMIT_PER_HOUR`), `expires_in_days?` (1-3650) | Issues a token; the plaintext `token` field is present only in this response. `201`. |
| DELETE | `/api/agent/tokens/{token_id}` | session or token | — | Revokes the token (`revoked_at`); the row is kept for audit. `204`. |
| GET | `/api/agent/audit` | session or token | `limit=100` (1-500) | Lists `agent_audit` rows for the caller, newest first. |

## Events (SSE)

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/events` | session or token | `text/event-stream` of sync updates for the caller, on their own Redis channel (`events:user:<id>`). Sends a `: ping` comment every 20s to keep proxies from closing an idle connection. |

## System

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/healthz` | none | `{"status": "ok"\|"degraded", "database": bool, "redis": bool, "version"}`. Used by the Docker healthcheck. |
| GET | `/api/docs` | none | Live Swagger UI over the app's own OpenAPI schema. |
| GET | `/api/openapi.json` | none | The OpenAPI schema itself. |

## curl example: token-authenticated message list

```
curl -s \
  -H "Authorization: Bearer lnv_..." \
  "http://localhost:8080/api/accounts/$ACCOUNT_ID/messages?folder_id=$FOLDER_ID&unread=true&limit=20"
```

Response shape:

```json
{
  "items": [
    {
      "id": "…", "account_id": "…", "thread_id": "…",
      "subject": "…", "from_name": "…", "from_addr": "…",
      "to": [{"name": null, "addr": "…"}],
      "snippet": "…", "internal_date": "2026-09-20T10:00:00Z",
      "sent_date": "2026-09-20T09:59:12Z", "size_bytes": 4821,
      "has_attachments": false, "body_state": "full",
      "seen": false, "flagged": false,
      "folder_ids": ["…"], "rfc822_message_id": "<…>"
    }
  ],
  "next_cursor": "MXwyMDI2LTA5LTIwVDEwOjAwOjAwKzAwOjAwfGE1Yzk…"
}
```

Every token-authenticated call against this route (and every other `/api/*` route) is
recorded in `agent_audit` with `tool` set to the literal method and path, e.g.
`"GET /api/accounts/2f1a.../messages"`, by the audit middleware, regardless of whether
the route itself checks a scope.

---
Maintained by [satuapps](https://github.com/satuapps).
