# Security Policy

## Supported versions

Lenovmail is pre-1.0 (`version = "0.1.0"` in `pyproject.toml`). There is one supported line:
the latest commit on `main`. Security fixes are not backported to tags or releases before 1.0.

## Reporting a vulnerability

Report privately through GitHub's security advisory flow, not a public issue:

<https://github.com/satuapps/lenovmail/security/advisories/new>

Include:

- affected component (`api`, `sync`, `providers`, `mcp`, `workers`, `web`) and version/commit
- reproduction steps or a proof of concept
- impact as you see it (data exposure, privilege escalation, credential leak, DoS, etc.)

Expect an initial response within 5 business days. If the report is confirmed, a fix is
targeted before public disclosure; coordinate disclosure timing with the maintainer via the
advisory thread. Do not open a public issue or PR that discloses the vulnerability before a
fix is available.

## Threat model

What Lenovmail defends against, in terms of what's actually implemented:

| Concern | Mechanism | Where |
| --- | --- | --- |
| IMAP/SMTP passwords and Microsoft OAuth tokens at rest | AES-256-GCM, keyed by `LENOVMAIL_SECRET_KEY` (32 random bytes, base64), with per-field AAD (`table:row_id:field`) so ciphertext can't be moved between rows or columns | `src/lenovmail/crypto.py` |
| User passwords at rest | argon2id (`argon2-cffi`) | `src/lenovmail/api/security.py` |
| Browser sessions | Opaque token in the `ln_session` cookie; the token itself is a Redis key (`session:<token>`) with a TTL (`LENOVMAIL_SESSION_TTL_DAYS`, default 14) — revoking a session or all of a user's sessions is a Redis delete, not a JWT that stays valid until expiry | `src/lenovmail/api/security.py` |
| Agent (AI) tokens | Shown once at creation (`lnv_...`); only a SHA-256 hash is stored, so a stolen database dump doesn't yield usable tokens | `src/lenovmail/api/security.py` |
| Agent authorization | Per-token scopes (`mail.read`, `mail.write`, `mail.send`, `mail.delete`, `mail.manage`) enforced on both transports — MCP tools and REST routes alike, including a `mail.read` gate on every route that returns mailbox content — plus an optional account allowlist, a mandatory-approval flag for sends, and an hourly send limit computed from outbox rows | `src/lenovmail/api/deps.py`, `src/lenovmail/mcp/auth.py`, `src/lenovmail/sync/outbox.py` |
| Agent accountability | Every token-authenticated REST call and every MCP tool call is written to `agent_audit`; the janitor trims old rows (`LENOVMAIL_JANITOR_AUDIT_RETENTION_DAYS`, default 90, `0` = keep forever) instead of the app doing it inline | `src/lenovmail/api/app.py`, `src/lenovmail/maintenance.py` |
| Host header spoofing | `TrustedHostMiddleware` bound to `LENOVMAIL_ALLOWED_HOSTS`, applied to the API and the MCP server, which is mounted on the same app | `src/lenovmail/api/app.py` |
| Stale/rejected credentials | The janitor (`maintenance.run_sweep`, arq cron job `janitor_sweep`, hourly at :17) re-checks accounts in `auth_error` against the real provider; accounts that stay rejected past `LENOVMAIL_JANITOR_ACCOUNT_GRACE_DAYS` (default 14) are disabled or deleted per `LENOVMAIL_JANITOR_ACCOUNT_ACTION` (default `disable`) | `src/lenovmail/maintenance.py` |
| Rendered HTML email | Sanitized server-side with `nh3` (`sync/normalize.py`, `nh3.clean(..., link_rel="noopener noreferrer nofollow")`) before storage, and again client-side with DOMPurify before rendering in the GUI | `src/lenovmail/sync/normalize.py`, `web/` |
| Transport | Lenovmail terminates plain HTTP; TLS is expected to be provided by a reverse proxy. The session cookie is marked `Secure` automatically once `LENOVMAIL_PUBLIC_BASE_URL` is `https://` | `src/lenovmail/config.py` (`cookies_secure`) |

## Non-goals

- **No multi-tenant isolation beyond account ownership.** Every account, message, and agent
  token belongs to exactly one `User` row and access checks are "does this user own this
  account", not a broader tenancy/organization boundary. Do not deploy Lenovmail as a shared
  service for mutually untrusted users without adding that layer yourself.
- **No sandboxing of HTML email beyond sanitization.** Rendered mail bodies go through `nh3`
  (server) and DOMPurify (client); there is no iframe sandbox, no CSP tuned per-message, and no
  remote-image proxy. Sanitization removes active content (scripts, event handlers, dangerous
  tags/attributes); it does not hide the fact that a message is HTML from a sender, and it does
  not block remote image loads used for read-tracking.
- Key rotation for `LENOVMAIL_SECRET_KEY` is not implemented: changing it makes every existing
  stored credential undecryptable (see `src/lenovmail/crypto.py`). Treat it as a value you back
  up, not one you rotate casually.

---

Maintained by [satuapps](https://satuapps.com).
