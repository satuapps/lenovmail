# Architecture

## Process topology

Lenovmail runs as two Python processes sharing one Postgres database and one Redis
instance:

| Process | Entry point | Responsibility |
|---|---|---|
| API | `uvicorn lenovmail.api.app:app` (or `lenovmail serve`) | REST API under `/api`, the built React GUI (`web/dist`, same origin, no CORS), and the MCP server mounted at `/api/mcp`. |
| Worker | `arq lenovmail.workers.main.WorkerSettings` | IMAP/Graph sync jobs, IMAP IDLE watchers, outbox delivery, the janitor sweep. |

Redis serves three distinct roles, all against `LENOVMAIL_REDIS_URL`
(default `redis://localhost:6379/0`): the arq job queue, pub/sub for GUI live-update
events (`sync/events.py`, channel `events:user:<user_id>`), and short-TTL keys
(`idle:watch:<account_id>`) that let `ensure_idle_watchers` tell whether an account
already has a live IDLE connection before enqueuing another one.

Postgres (`LENOVMAIL_DATABASE_URL`, default
`postgresql+asyncpg://lenovmail:lenovmail@localhost:5432/lenovmail`) is the only
persistent store for structured data. Raw MIME bodies live on disk under
`LENOVMAIL_BLOB_ROOT` (default `./var/blobs`), addressed by content hash — see
[Blob storage](#blob-storage-content-addressed).

```mermaid
flowchart LR
    GUI["React GUI (web/dist)"] -->|same-origin| API
    Agent["AI agent (REST or MCP)"] --> API

    subgraph Processes
        API["API process: FastAPI serving /api and /api/mcp"]
        Worker["Worker process: arq sync, IDLE, outbox, janitor"]
    end

    subgraph State
        PG[(Postgres)]
        Redis[(Redis)]
        Blobs[("Blob store at LENOVMAIL_BLOB_ROOT")]
    end

    API -->|SQL| PG
    API -->|enqueue jobs, pub/sub| Redis
    API -->|read raw messages| Blobs
    Worker -->|SQL| PG
    Worker -->|dequeue, pub/sub, IDLE locks| Redis
    Worker -->|write content-addressed .eml.gz| Blobs
    Worker -->|IMAP / Graph| Providers["Mail providers"]
```

The API process itself also holds an arq pool (`app.state.arq`, created in
`lifespan()` in `api/app.py`) so REST endpoints can enqueue jobs directly — e.g. a
manual "sync now" click or `deliver_outbox` after a user sends mail — without going
through the worker's cron schedule.

## Data model

Every table below is defined in `src/lenovmail/models/`. `accounts` is the root; each
sync path (`imap_sync.py` / `graph_sync.py`) writes into `folders`, `messages`,
`mailbox_messages`, `message_bodies`, and `attachments` through the shared writer in
`sync/store.py`.

| Table | Purpose | Key columns |
|---|---|---|
| `accounts` | One mailbox (IMAP or Graph) owned by a user. | `provider` (`imap`/`graph`), `status` (`active`/`auth_error`/`error`/`disabled`), `sync_interval_s`, `last_sync_at`, `invalid_since` |
| `imap_settings` / `graph_settings` | Per-provider connection details, 1:1 with `accounts`. | `imap_settings.password_enc`, `smtp_password_enc`; `graph_settings.token_cache_enc`, `folders_delta_link_enc` |
| `folders` | One IMAP mailbox or Graph mail folder. | `role` (`inbox`/`sent`/`drafts`/`trash`/`junk`/`archive`/`other`), `uidvalidity`/`uidnext`/`highestmodseq` (IMAP cursor), `delta_link_enc` (Graph cursor), `sync_state`, `sync_pass` |
| `messages` | One physical message per account, deduplicated. | `dedup_hash` (unique per `account_id`), `thread_id`, `blob_sha256`, `body_state` (`none`/`partial`/`full`), envelope fields (`subject`, `from_addr`, `to_addrs`, …) |
| `mailbox_messages` | Placement of a message inside a folder — a message can sit in several folders at once. | `folder_id`, `message_id`, `remote_uid` (IMAP) / `remote_item_id` (Graph), `flag_seen`/`flag_flagged`/`flag_answered`/`flag_draft`/`flag_deleted`, `modseq` |
| `message_bodies` | Parsed text/HTML body, 1:1 with `messages`. | `body_text`, `body_html` |
| `message_search` | Full-text search index. | `tsv` (GIN-indexed `tsvector`, populated from subject/sender at header time and from the body once it arrives) |
| `message_refs` | Ordered `References`/`In-Reply-To` header values, used for threading. | `message_id`, `position`, `ref` |
| `attachments` | Attachment metadata; content is read back from the blob store via `part_path`. | `message_id`, `part_path`, `filename`, `mime_type`, `content_id`, `is_inline` |
| `threads` | Conversation grouping across messages. | `subject_norm`, `last_message_at`, `message_count` |
| `outbox` | Outgoing message queue. | `status` (`pending_approval`/`queued`/`sending`/`sent`/`failed`), `payload` (JSONB), `locked_at`, `attempts` |
| `sync_runs` | Audit trail of every sync cycle, per folder. | `kind` (`full`/`incremental`), `added`/`updated`/`removed`, `error` |
| `agent_tokens` | Scoped bearer tokens issued to AI agents. | `token_hash`, `scopes`, `account_ids` (`NULL` = all of the owner's accounts), `require_send_approval`, `send_limit_per_hour`, `revoked_at` |
| `agent_audit` | One row per agent API/MCP call. | `token_id`, `tool`, `target_ids`, `outcome` (`ok`/`denied`/`error`) |

A message is never duplicated per folder: `messages` holds one row per distinct
message (by `dedup_hash`), and `mailbox_messages` links it into every folder it
appears in (e.g. Inbox and an "All Mail" label). Flags are stored per placement, not
per message, because IMAP flags are folder-scoped.

```mermaid
erDiagram
    FOLDERS ||--o{ MAILBOX_MESSAGES : "contains"
    MESSAGES ||--o{ MAILBOX_MESSAGES : "placed as"
    MESSAGES ||--o| MESSAGE_BODIES : "has"
    MESSAGES ||--o{ ATTACHMENTS : "has"
```

## IMAP sync cycle

`sync/imap_sync.py` runs one cycle per folder (`sync_folder`), called from
`workers/tasks.py::_sync_imap_account` for every folder of an account, in this fixed
order:

1. **UIDVALIDITY check.** If the server's `UIDVALIDITY` differs from the stored value
   (or there is no stored value yet), every `mailbox_messages` row for the folder is
   deleted and the cycle runs as a full sync — old UIDs are no longer meaningful once
   `UIDVALIDITY` changes.
2. **New UIDs.** `UID SEARCH ALL` on a full sync, or `UID <uidnext>:*` on an
   incremental one.
3. **Flag refresh.** If the server advertises `CONDSTORE` and a `highestmodseq` is
   already stored, only messages with `CHANGEDSINCE <highestmodseq>` are re-fetched.
   Without `CONDSTORE`, or every `LENOVMAIL_FLAG_REFRESH_EVERY` cycles (default `6`,
   tracked per folder as `folders.sync_pass % flag_refresh_every == 0`), flags for the
   entire folder are refetched and deletions are checked as a side effect.
4. **Header pass.** New UIDs are fetched in batches of `LENOVMAIL_HEADER_FETCH_CHUNK`
   (default `200`) via `FETCH ENVELOPE FLAGS INTERNALDATE`, written into `messages` /
   `mailbox_messages` / `message_search`, and assigned a provisional thread from the
   normalized subject (headers alone don't carry `References` yet).
5. **Deletion detection.** On a full sync or a flag-refresh cycle, the server's UID set
   is diffed against `mailbox_messages`; placements missing from the server are
   deleted, and messages left with no placement in any folder are purged
   (`store.delete_missing_placements` / `store.purge_orphan_messages`).
6. **Folder cursor update.** `uidvalidity`, `uidnext`, `highestmodseq`, `sync_pass`,
   and `last_synced_at` are written back to `folders`.

Body backfill is a separate pass (`fetch_bodies`, invoked right after the header pass
in `_sync_imap_account`), so the message list is usable long before every body is
downloaded. It selects messages with `body_state = 'none'`, newest first, up to
`LENOVMAIL_BODY_BACKFILL_MAX_PER_RUN` (default `500`) per call, in batches of
`LENOVMAIL_BODY_FETCH_CHUNK` (default `25`). Messages larger than
`LENOVMAIL_BODY_FULL_FETCH_MAX_BYTES` (default `26214400`, 25 MiB) are fetched as
header + first MIME part only and marked `body_state = 'partial'` so they are not
retried on every run.

### Blob storage (content-addressed)

`blobs.py` stores the raw MIME bytes of every fetched message on disk under
`LENOVMAIL_BLOB_ROOT`, addressed by `sha256(raw_bytes)`:

```
BLOB_ROOT/<hex[0:2]>/<hex[2:4]>/<hex>.eml.gz
```

Two folders — or two accounts — that happen to contain byte-identical messages share
one file on disk. Writes are atomic (`gzip` to a `.tmp<pid>` file, then `os.replace`),
and a blob is never deleted just because one message referencing it was deleted: v1
has no garbage collection for blobs.

## Graph sync cycle

`sync/graph_sync.py` mirrors the same header-then-body split but is driven by
Microsoft Graph's delta model instead of UID/CONDSTORE:

- **Folder delta.** `sync_folders` walks `/me/mailFolders/delta` using the encrypted
  link in `accounts` (`GraphSettings.folders_delta_link_enc`) to keep the folder
  hierarchy and roles in sync.
- **Per-folder message delta.** `sync_folder` calls
  `/me/mailFolders/{id}/messages/delta` with the stored `folders.delta_link_enc`. A
  delta response only mentions items that changed since the last call — an item not
  mentioned still exists, so deletion is driven by explicit `@removed` entries, not a
  set-difference like IMAP.
- **410 / expired delta token.** If Graph answers `410` or one of
  `syncStateNotFound`, `resyncRequired`, `invalidDeltaToken`,
  `ErrorInvalidDeltaToken` (`is_delta_expired`), the cycle restarts from scratch: a
  full delta call with `link=None`, which enumerates the folder's entire contents. On
  a full delta, anything not present in the response is deleted
  (`store.delete_missing_placements`), since a full delta is authoritative for the
  folder's current contents.
- **Body fetch.** `fetch_bodies` downloads `/$value` (raw MIME) for messages with
  `body_state = 'none'`, up to `LENOVMAIL_GRAPH_BODY_CONCURRENCY` (default `6`)
  concurrent requests. If Graph refuses `$value` (`400`/`404`/`413`, or one of
  `ErrorInvalidIdMalformed`, `ErrorItemNotFound`, `ErrorMailboxStoreUnavailable` —
  `is_body_refused`), the message is instead assembled from the message JSON plus its
  attachment list (`store_partial_message`) and marked `body_state = 'partial'`.

Every `delta_link` and `folders_delta_link` is stored encrypted
(`crypto.account_aad`), the same as IMAP passwords — see
[Credentials and encryption](#credentials-and-encryption).

## Outbox delivery

`sync/outbox.py::send_pending` sends the oldest `queued` rows for one account, up to
`limit` (10, called from `workers/tasks.py::deliver_outbox`). Rows are claimed before
they are sent:

```sql
SELECT * FROM outbox
WHERE account_id = :account_id AND status = 'queued'
ORDER BY created_at ASC
LIMIT :limit
FOR UPDATE SKIP LOCKED
```

`FOR UPDATE SKIP LOCKED` lets multiple `deliver_outbox` jobs run concurrently against
the same account (e.g. a manual retry racing the next scheduled attempt) without
either blocking on the other's row lock or double-sending a message: a row already
claimed by one transaction is invisible to the other, which skips it and picks
the next one. After the `SELECT`, each row is stamped `status = 'sending'` and
`locked_at = now()`, committed, and only then handed to `send_one`. A claim older than
`CLAIM_TIMEOUT_S` (900s) is treated as abandoned and reset to `queued` by
`_release_stale_claims` before the next claim attempt — this recovers rows left behind
by a worker process that died mid-send.

## Credentials and encryption

Every stored secret — IMAP/SMTP passwords, the Graph MSAL token cache, and Graph
delta-link cursors — is encrypted with AES-256-GCM in `crypto.py` before it is
written to Postgres. The key comes from `LENOVMAIL_SECRET_KEY` (base64 of exactly 32
bytes; there is no default and no key rotation in v1 — generate it with
`python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"`).

The additional authenticated data (AAD) for each ciphertext is
`f"{table}:{row_id}:{field}"` (`crypto.account_aad`), e.g.
`accounts:<uuid>:graph_delta_link`. Binding the AAD to the exact row and column means
ciphertext copied from one account's row into another's — or from one column into
another — fails to decrypt (`InvalidTag`) instead of silently succeeding with the
wrong key context. The stored format is `version_byte(1) + nonce(12) + ciphertext+tag`.

---
Maintained by [satuapps](https://github.com/satuapps).
