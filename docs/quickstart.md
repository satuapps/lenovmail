# Quickstart

This walks through a clean checkout of Lenovmail to a working inbox in the GUI: containers up,
admin user created, one mailbox connected, first sync finished, and one REST call made with an
agent token. Every command below is copy-pasteable against this repository as-is.

## Prerequisites

Pick one path.

| Path | Requires |
| --- | --- |
| Docker (recommended) | Docker Engine + the `docker compose` plugin |
| Local dev, no Docker | [`uv`](https://docs.astral.sh/uv/) (Python 3.13 project), Node.js 22, plus Docker only for `db`/`redis` (or your own Postgres 17 / Redis 8) |

The rest of this page uses the Docker path. The local-dev equivalent is in
[configuration.md](configuration.md) and the `Development` section of the repo
README.

## 1. Clone and configure

```bash
git clone https://github.com/satuapps/lenovmail.git
cd lenovmail
cp .env.example .env
```

Generate `LENOVMAIL_SECRET_KEY` — it's the AES-256-GCM key that encrypts every stored IMAP/SMTP
password and Microsoft token, so leaving it blank makes the API refuse to start:

```bash
python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
```

Paste the output into `.env` as `LENOVMAIL_SECRET_KEY=...`. Everything else in `.env.example` has
a working default for a local run.

## 2. Start the stack

```bash
docker compose up -d --build
```

This builds the API image (Vite build of `web/` copied into `/app/static`, then the Python
package), and starts four services from `docker-compose.yml`:

| Service | What it is | Host port |
| --- | --- | --- |
| `db` | PostgreSQL 17 | `5433` → container `5432` |
| `redis` | Redis 8.8 | `6380` → container `6379` |
| `api` | FastAPI app (GUI + REST + MCP), runs `alembic upgrade head` before serving | `8080` → container `8000` |
| `worker` | arq worker (sync, IDLE, body backfill, outbox delivery, janitor) | none published |

`db` and `redis` publish on `5433`/`6380` instead of the standard ports because a local Postgres
or Redis install commonly already owns `5432`/`6379` on the host. `api` and `worker` read
`LENOVMAIL_DATABASE_URL`/`LENOVMAIL_REDIS_URL` pointed at the in-network service names (`db`,
`redis`), not those host ports — that override lives directly in `docker-compose.yml` and doesn't
need `.env` changes.

Check everything came up:

```bash
docker compose ps
curl -s http://localhost:8080/api/healthz
```

`healthz` returns `{"status":"ok","database":true,"redis":true,"version":"0.1.0"}` once the
database and Redis pings succeed.

## 3. Create the admin user

```bash
docker compose exec api uv run lenovmail bootstrap-admin --email admin@example.com
```

Without `--password`, a random one is generated and printed once:

```
admin ready: admin@example.com
password: <generated>
```

Copy it now — running `bootstrap-admin` again for the same email resets the password without
re-printing it unless you pass `--password` explicitly.

## 4. Sign in

Open <http://localhost:8080>. Sign in with the email/password from step 3. This sets the
`ln_session` HttpOnly cookie (14-day TTL by default, `LENOVMAIL_SESSION_TTL_DAYS`). API docs are
served at <http://localhost:8080/api/docs>.

## 5. Add a mailbox

**Accounts → Add account** opens a 3-step wizard: discovery, credentials, confirmation. It calls
`POST /api/accounts/discover` first, which runs a tiered detection chain in this order:

1. Bundled overrides (`src/lenovmail/autoconfig/providers.yaml`) for `gmail.com`/`googlemail.com`,
   `outlook.com`/`hotmail.com`/`live.com`/`msn.com`/`outlook.co.id`, `yahoo.com`/`ymail.com`/
   `rocketmail.com`, `icloud.com`/`me.com`/`mac.com`, `zoho.com`/`zohomail.com`.
2. `https://autoconfig.<domain>/mail/config-v1.1.xml` (Thunderbird autoconfig v1.1).
3. `https://<domain>/.well-known/autoconfig/mail/config-v1.1.xml`.
4. The Mozilla ISPDB (`autoconfig.thunderbird.net`).
5. MX lookup: if the MX record ends in `.mail.protection.outlook.com` the domain is Microsoft
   365 even without an `outlook.com` address; otherwise the MX target's registrable domain is
   retried against the ISPDB.
6. SRV records (RFC 6186: `_imap._tcp`, `_imaps._tcp`, `_submission._tcp`, `_submissions._tcp`).
7. Direct hostname probing (`imap.<domain>`, `mail.<domain>`, etc. — connects and reads the
   banner).

What this means for three common cases:

- **A Gmail address** matches the bundled override immediately: IMAP `imap.gmail.com:993`
  (SSL), SMTP `smtp.gmail.com:465` (SSL), and a warning that Gmail needs an **app password**, not
  your account password (create one after enabling 2-step verification; a plain account password
  is rejected). The same app-password requirement applies to the Yahoo and iCloud overrides.
- **An Outlook/Hotmail/Live/MSN address, or any custom domain whose MX points at Microsoft 365**,
  resolves to `provider: graph`. There's no password field for this — the wizard shows a
  **Connect with Microsoft** button that starts the OAuth flow instead. This requires
  `LENOVMAIL_MS_CLIENT_ID`/`LENOVMAIL_MS_CLIENT_SECRET` to be set (empty by default); with them
  unset, `POST /api/accounts` for a Graph account fails with `400 LENOVMAIL_MS_CLIENT_ID/SECRET
  are not set; Microsoft accounts can't be added`. See [deployment.md](deployment.md) for
  registering an Azure app. Skip this case for a first local run — use IMAP instead.
- **A custom domain on a generic mail server** (no bundled override, no MX pointing at Microsoft)
  falls through to MX+ISPDB, then SRV, then the hostname probe. If none of those resolve
  anything, discovery returns `source: "none"` and the wizard's credential step expects host,
  port, and security (`ssl`/`starttls`/`none`) to be filled in by hand — this is exactly the path
  the local GreenMail mailbox in step 7 takes.

Enter the account password (or app password) on the credentials step and confirm. On create, the
API immediately verifies the IMAP login and enqueues the first `sync_account` job.

![Accounts page with one account and Sync now, Test connection, Disable, and Delete buttons](images/accounts.png)

## 6. Wait for the first sync

The initial sync starts as soon as the account is created — there's no need to wait for the
5-minute `LENOVMAIL_SYNC_INTERVAL_S` cycle. It discovers folders (IMAP `LIST` + SPECIAL-USE),
fetches headers for a fast listing, then backfills bodies in the background. Watch it finish
either in the GUI (folder unread/total counts populate) or from the CLI:

```bash
docker compose exec api uv run lenovmail show-account admin@example.com
```

This prints per-folder message counts and the last three sync runs. Once it shows non-zero
message counts, open the account in the GUI — the message list and reading pane are live.

## 7. No real mailbox? Use the GreenMail test mailbox

GreenMail is a disposable fake IMAP/SMTP server for trying Lenovmail without any real
credentials.

```bash
docker compose -f docker-compose.test.yml up -d
```

This starts one `greenmail` container (`greenmail/standalone:2.1.14`) with SMTP on `3025`, IMAP
on `3143`, IMAPS on `3993`, SMTPS on `3465`, and its own status API on `8081`. Auth is disabled —
any username/password is accepted, and a message sent to an address auto-creates that mailbox.

Seed it with synthetic mail (the script needs `uv`, so run it on the host, not inside a
container):

```bash
uv run python scripts/seed_greenmail.py --count 200
```

This logs into IMAP as `demo`/`demo` on `127.0.0.1:3143` and appends 200 generated messages to
`INBOX`, plus three fixed markers used by the project's own verification: `Invoice ACME 42`
(body contains "payment overdue"), `Q3 Report` (has a `report.pdf` attachment), and a reply to it
(`Re: Q3 Report`, threaded via `In-Reply-To`/`References`). Run `--help` for the other flags
(`--count`, `--user`, `--mailbox`, `--subject` for a single custom message).

Add the account in the wizard using **email `demo@localhost`** — `localhost` isn't a real domain,
so auto-discovery returns nothing and you fill in the credentials step manually:

| Field | Value |
| --- | --- |
| IMAP host / port | `localhost` / `3143` |
| IMAP security | `none` |
| SMTP host / port | `localhost` / `3025` |
| SMTP security | `none` |
| Username / password | `demo` / `demo` |

GreenMail creates a user from the destination address on delivery, so mail addressed to
`demo@localhost` may land in that auto-created mailbox — that's test-server behavior, not
something Lenovmail does.

## 8. Issue an agent token and call the API

Agent tokens are how AI agents (REST or MCP) authenticate, separately from the browser session.
Issue one from the CLI — the token is printed once and only its SHA-256 hash is stored:

```bash
docker compose exec api uv run lenovmail agent-token \
  --email admin@example.com --name quickstart --scopes mail.read --days 90
```

Output is the bare token, e.g. `lnv_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`. Call the REST API with it:

```bash
curl -s http://localhost:8080/api/accounts \
  -H "Authorization: Bearer lnv_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

This returns the JSON array of accounts owned by `admin@example.com`, each with `unread`/`total`
counts and `last_sync_at`. The same token works against `/api/mcp` (Streamable HTTP) for MCP
clients — see [agents.md](agents.md).

## What to read next

- [architecture.md](architecture.md) — how sync, storage, and delivery fit together.
- [configuration.md](configuration.md) — every `LENOVMAIL_*` environment variable and its default.
- [agents.md](agents.md) — agent token scopes, limits, and the MCP tool list.
- [api.md](api.md) — full REST endpoint reference.
- [operations.md](operations.md) — day-2 operation: the janitor sweep, manual sync, account
  lifecycle.
- [deployment.md](deployment.md) — running this for real: TLS, scaling, secrets, Microsoft Graph
  app registration.

---
Maintained by [satuapps](https://github.com/satuapps).
