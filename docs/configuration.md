# Configuration

All settings are environment variables prefixed `LENOVMAIL_`, loaded by `Settings` in
[`src/lenovmail/config.py`](../src/lenovmail/config.py) (pydantic-settings). They can come from
the process environment or a `.env` file in the working directory (`env_file=".env"`). Unknown
variables are ignored (`extra="ignore"`).

`docker compose` reads `.env` too (`env_file: {path: .env, required: false}` on `api` and
`worker`), then overrides three of them with in-network values (`environment: &appenv` in
[`docker-compose.yml`](../docker-compose.yml)):

| Variable | `.env` (host tooling) | docker compose override |
|---|---|---|
| `LENOVMAIL_DATABASE_URL` | `postgresql+asyncpg://lenovmail:lenovmail@localhost:5433/lenovmail` | `postgresql+asyncpg://lenovmail:lenovmail@db:5432/lenovmail` |
| `LENOVMAIL_REDIS_URL` | `redis://localhost:6380/0` | `redis://redis:6379/0` |
| `LENOVMAIL_BLOB_ROOT` | `./var/blobs` | `/var/lib/lenovmail/blobs` |

The host ports (5433, 6380) exist only because a local Postgres/Redis on the default ports would
otherwise shadow the container binds — see the comments in `docker-compose.yml`. Every other
variable below applies identically to both the API and worker containers, since they share the
same `environment` block.

## The three that break things

| Variable | What goes wrong |
|---|---|
| `LENOVMAIL_SECRET_KEY` | Encrypts every stored IMAP/SMTP password, Microsoft Graph token, and delta link (`src/lenovmail/crypto.py`). Rotating or losing it makes all existing credentials undecryptable — every account has to be re-authenticated. Never regenerate it against a database that already has accounts. |
| `LENOVMAIL_ALLOWED_HOSTS` | Passed straight to Starlette's `TrustedHostMiddleware` (`api/app.py`). Any request whose `Host` header is not in this comma-separated list is rejected with `400 Bad Request` before it reaches a route. Add every hostname (with port, if non-default) the API is reachable on — reverse proxy hostnames included. |
| `LENOVMAIL_PUBLIC_BASE_URL` | Two effects: it is the base for the Microsoft OAuth `redirect_uri` (`providers/graph.py: redirect_uri()`, must exactly match the redirect URI registered on the Azure app), and `cookies_secure` (`config.py`) is `True` only when it starts with `https://` — that flag sets the `Secure` attribute on the session cookie. An `http://` value in production means the browser will still send the session cookie over plaintext. |

## Connection

| Variable | Default | Controls | Change it when |
|---|---|---|---|
| `LENOVMAIL_DATABASE_URL` | `postgresql+asyncpg://lenovmail:lenovmail@localhost:5432/lenovmail` | SQLAlchemy async engine DSN. | Pointing at a managed Postgres instance or a non-default user/db name. |
| `LENOVMAIL_REDIS_URL` | `redis://localhost:6379/0` | Redis connection used for arq job queues, IMAP IDLE fan-out events, session storage, and login rate limiting. | Pointing at a managed Redis, or separating DB index. |
| `LENOVMAIL_BLOB_ROOT` | `./var/blobs` | Filesystem root for raw message/attachment blobs (`blobs.py`, content-addressed by SHA-256). | Always, outside of `docker compose` (the container mounts a named volume here). Must be a path the API and worker processes both have read/write access to — they share the store. |

## Security & sessions

| Variable | Default | Controls | Change it when |
|---|---|---|---|
| `LENOVMAIL_SECRET_KEY` | `""` | AES key material for `crypto.py` (see above). Empty by default; the app does not refuse to start with an empty key, but nothing decrypts correctly. | Once, at first deployment. Generate with `python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`. |
| `LENOVMAIL_PUBLIC_BASE_URL` | `http://localhost:8080` | OAuth redirect URI base and the cookie `Secure` flag (see above). | Any deployment reachable at a different origin than `localhost:8080`. |
| `LENOVMAIL_ALLOWED_HOSTS` | `localhost:8080,127.0.0.1:8080,localhost,127.0.0.1` | `TrustedHostMiddleware` allow-list, comma-separated, no scheme. | Every deployment behind a real hostname or reverse proxy. |
| `LENOVMAIL_SESSION_TTL_DAYS` | `14` | Lifetime of the browser login session stored in Redis (`api/security.py`). | Tightening or loosening how long a signed-in browser session survives without re-login. |

## Microsoft Graph (OAuth)

| Variable | Default | Controls | Change it when |
|---|---|---|---|
| `LENOVMAIL_MS_CLIENT_ID` | `""` | Azure AD application (client) ID used by MSAL. | Adding Microsoft 365 / Outlook.com account support. Without it, adding a `graph` account returns `400` ("LENOVMAIL_MS_CLIENT_ID is not set"). |
| `LENOVMAIL_MS_CLIENT_SECRET` | `""` | Azure AD client secret. Empty means the registration has none, and `build_msal_app()` (`providers/graph.py`) uses a public client instead of a confidential one; the OAuth flow is otherwise identical. | The app registration is a confidential client (platform **Web** with a secret). Leave empty for a **Mobile and desktop applications** registration. |
| `LENOVMAIL_MS_AUTHORITY` | `https://login.microsoftonline.com/common` | MSAL authority URL. | Restricting sign-in to a single Azure tenant (use `.../<tenant-id>` instead of `/common`). |
| `LENOVMAIL_MS_SCOPES` | `https://graph.microsoft.com/.default` | Scopes requested at consent and on every silent refresh, split on commas or whitespace. `.default` means "whatever this registration already has consent for"; a granular list passes consent but is rejected at refresh time with `AADSTS70000`, which strands the account in `auth_error`. `openid`, `profile` and `offline_access` are dropped because msal adds them itself. | The registration must request less than it holds — see [deployment.md](deployment.md#scopes-and-aadsts70000). |

## Discovery & sync tuning

These govern the default IMAP polling and Graph delta-sync loops (`sync/imap_sync.py`,
`sync/graph_sync.py`) and connection pooling (`providers/imap_pool.py`).

| Variable | Default | Controls | Change it when |
|---|---|---|---|
| `LENOVMAIL_SYNC_INTERVAL_S` | `300` | Default per-account IMAP polling interval (seconds), used when an account doesn't set its own `sync_interval_s` (see [Per-account settings](#per-account-settings-not-env-vars)). | Tuning the platform-wide default poll cadence. |
| `LENOVMAIL_GRAPH_POLL_INTERVAL_S` | `60` | Interval between Graph delta-sync cycles per account. | Graph accounts need fresher/staler mail state; Graph delta queries are cheaper than IMAP polls so this defaults lower than `SYNC_INTERVAL_S`. |
| `LENOVMAIL_IMAP_MAX_CONN_PER_ACCOUNT` | `3` | Max concurrent IMAP connections held per account in the pool. | Provider connection limits are hit (`too many simultaneous connections` errors), or more headroom is needed for bulk header/body backfill. |
| `LENOVMAIL_HEADER_FETCH_CHUNK` | `200` | Messages per `FETCH` batch when pulling headers during a sync cycle. | Tuning IMAP round-trips vs. per-request payload size for slow or rate-limited servers. |
| `LENOVMAIL_BODY_FETCH_CHUNK` | `25` | Messages per batch when fetching full bodies. | Same trade-off as above, applied to the heavier body fetch path. |
| `LENOVMAIL_BODY_FULL_FETCH_MAX_BYTES` | `26214400` (25 MiB) | Upper size limit for a message body fetched in full; larger messages are handled differently by the body backfill path. | Mailboxes routinely carry larger single messages that should still be fetched in full. |
| `LENOVMAIL_BODY_BACKFILL_MAX_PER_RUN` | `500` | Max messages backfilled with full bodies in one worker run. | Large existing mailboxes are backfilling too slowly (raise) or hammering the IMAP server (lower). |
| `LENOVMAIL_GRAPH_BODY_CONCURRENCY` | `6` | Concurrent Graph API requests when fetching message bodies. | Hitting Graph throttling (`429`) — lower it; otherwise raise for faster backfill. |
| `LENOVMAIL_FLAG_REFRESH_EVERY` | `6` | Every Nth sync cycle re-syncs message flags (seen/flagged) in addition to new mail, since flag changes are cheaper to skip most cycles. | Flag state (read/unread) in the GUI is lagging behind the mail provider more than desired. |

## Workers

| Variable | Default | Controls | Change it when |
|---|---|---|---|
| `LENOVMAIL_WORKER_MAX_JOBS` | `10` | arq `max_jobs`: concurrent jobs one worker process runs (`workers/main.py`). | Scaling worker throughput up (more CPU/network headroom) or down (resource-constrained host). |
| `LENOVMAIL_WORKER_JOB_TIMEOUT_S` | `900` | Per-job timeout before arq kills and retries it. | A sync or backfill job on a very large mailbox needs more time than 15 minutes, or a hung job should be killed sooner. |
| `LENOVMAIL_IDLE_RENEW_S` | `1500` | Length of one IMAP IDLE cycle before the connection is dropped and a fresh one issued (RFC 2177 recommends renewing IDLE below 29 minutes). | Rarely; only if a specific IMAP server times out IDLE sooner than 25 minutes. |
| `LENOVMAIL_SMTP_TIMEOUT_S` | `30` | Socket timeout for outbound SMTP connections (`providers/smtp.py`, outbox delivery). | Sending through a slow or high-latency SMTP relay. |

## Agents (REST/MCP tokens)

| Variable | Default | Controls | Change it when |
|---|---|---|---|
| `LENOVMAIL_AGENT_SEND_LIMIT_PER_HOUR` | `20` | Default outbound-send rate limit for a newly created agent token, applied when `AgentTokenCreate.send_limit_per_hour` is not given (`api/routes/agent.py`). Each token can still be created with its own explicit limit. | Changing the platform-wide default before any explicit per-token overrides are considered. |

## Janitor

Configures `maintenance.run_sweep`, which runs as the arq cron job `janitor_sweep` (hourly, at
minute 17 — see `workers/main.py`) and on demand via `uv run lenovmail janitor [--action disable|delete]`.

| Variable | Default | Controls | Change it when |
|---|---|---|---|
| `LENOVMAIL_JANITOR_ACCOUNT_GRACE_DAYS` | `14` | Days an account may sit in `auth_error` (measured from `accounts.invalid_since`, stamped on the first confirmed rejection) before it is retired. | Credentials are known to rotate slowly/quickly for the accounts in this deployment. |
| `LENOVMAIL_JANITOR_ACCOUNT_ACTION` | `disable` | What "retired" means once the grace period expires: `disable` sets `status=disabled` (recoverable), `delete` drops the account row and its mail by cascade. | Only set to `delete` deliberately — it is destructive and irreversible. |
| `LENOVMAIL_JANITOR_CHECK_BATCH` | `50` | Max number of `auth_error` accounts re-checked against the provider in one sweep. | Many accounts are quarantined at once and a single hourly sweep isn't clearing the backlog. |
| `LENOVMAIL_JANITOR_TOKEN_RETENTION_DAYS` | `7` | Days an expired or revoked `agent_tokens` row is kept before deletion. | Audit/compliance requirements need tokens retained longer (or shorter) after revocation. |
| `LENOVMAIL_JANITOR_AUDIT_RETENTION_DAYS` | `90` | Days `agent_audit` rows are kept. `0` disables audit trimming entirely (keep forever). | Storage growth from audit logging, or a compliance requirement to retain agent call history longer. |

## Logging

| Variable | Default | Controls | Change it when |
|---|---|---|---|
| `LENOVMAIL_LOG_LEVEL` | `INFO` | stdlib/structlog level passed to `configure_logging()`. **Only the arq worker process reads this** (`workers/main.py: on_startup` calls `configure_logging(settings.log_level)`); the API process and every `lenovmail` CLI command call `configure_logging()` with no argument and always log at `INFO`. | Debugging worker-side sync/outbox behavior. Has no effect on API or CLI log verbosity. |

## Per-account settings (not env vars)

Some behavior is per-account, stored on the `accounts` row, not global configuration:

| Setting | Where it lives | How it's set |
|---|---|---|
| Sync interval | `accounts.sync_interval_s` (seconds, DB default `300`) | `POST /api/accounts` (`sync_interval_s`, defaults to `LENOVMAIL_SYNC_INTERVAL_S` if omitted) or `PATCH /api/accounts/{id}` (`sync_interval_s`). Validated to the range 60–86400 seconds by the API schema. |
| Account status | `accounts.status` (`active`, `auth_error`, `error`, `disabled`) | Set to `active`/`disabled` explicitly via `PATCH /api/accounts/{id}` (`status`); set to `auth_error`/`error` automatically by the sync path and the janitor as accounts fail or recover. |
| Append sent mail to Sent folder | `accounts.append_to_sent` (boolean, DB default `true`) | Not exposed through the API or GUI. Set at row creation by the migration default; changing it for an existing account requires a direct database update. When `true`, `sync/outbox.py` copies a successfully-sent message into the account's Sent folder after SMTP delivery; a copy failure is logged but never fails the send. |

## Minimal production `.env`

Everything else has a workable default. At minimum, set:

```bash
# Required — see "The three that break things" above.
LENOVMAIL_SECRET_KEY=<base64 of 32 random bytes>
LENOVMAIL_PUBLIC_BASE_URL=https://mail.example.com
LENOVMAIL_ALLOWED_HOSTS=mail.example.com

# Required if any real Postgres/Redis instance isn't the docker-compose default.
LENOVMAIL_DATABASE_URL=postgresql+asyncpg://lenovmail:<password>@db-host:5432/lenovmail
LENOVMAIL_REDIS_URL=redis://redis-host:6379/0

# Required only to support Microsoft 365 / Outlook.com accounts.
LENOVMAIL_MS_CLIENT_ID=<azure-app-client-id>
# Only for a confidential app registration; omit it for one created without a secret.
LENOVMAIL_MS_CLIENT_SECRET=<azure-app-client-secret>
```

---
Maintained by [satuapps](https://satuapps.com).
