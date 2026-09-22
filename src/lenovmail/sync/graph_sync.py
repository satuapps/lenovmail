# Lenovmail — authored by satuapps
"""Microsoft Graph sync engine: folder delta, message delta, body, and write-back.

Key differences from IMAP:

* Graph uses **per-folder delta** (`/me/mailFolders/{id}/messages/delta`). The long-lived
  sync token lives inside `@odata.deltaLink`, stored encrypted in `folders.delta_link_enc`;
  an expired deltaLink responds with 410/`syncStateNotFound` and the cycle restarts from
  scratch.
* Incremental delta only mentions items that **changed**, so items not mentioned still
  exist. Deletion only follows `@removed` entries — not a set-difference like IMAP.
* Body is fetched from `/$value` (raw MIME). If Graph refuses (large message/invalid ID
  for `$value`), metadata is fetched from the message JSON + attachment list and marked
  `partial`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import blobs
from ..config import settings
from ..crypto import account_aad, decrypt, encrypt
from ..logging import get_logger
from ..models import Account, Folder, GraphSettings, MailboxMessage, Message, SyncRun
from . import store
from .events import publish
from .imap_sync import SyncStats, mark_folder_error
from .normalize import (
    ParsedAttachment,
    ParsedMessage,
    html_to_text,
    normalize_subject,
    parse_message,
    sanitize_html,
)

log = get_logger(__name__)

FOLDERS_DELTA_FIELD = "graph_folders_delta_link"
FOLDER_DELTA_FIELD = "graph_delta_link"

# Graph codes meaning "delta token is no longer valid" -> restart the cycle from scratch.
DELTA_EXPIRED_CODES = frozenset(
    {"syncStateNotFound", "resyncRequired", "invalidDeltaToken", "ErrorInvalidDeltaToken"}
)
BODY_FALLBACK_CODES = frozenset(
    {"ErrorInvalidIdMalformed", "ErrorItemNotFound", "ErrorMailboxStoreUnavailable"}
)

WELL_KNOWN_ROLES = {
    "inbox": "inbox",
    "sentitems": "sent",
    "drafts": "drafts",
    "deleteditems": "trash",
    "junkemail": "junk",
    "archive": "archive",
    "msgfolderroot": "other",
}

NAME_ROLES = (
    ("inbox", "inbox"),
    ("sent", "sent"),
    ("draft", "drafts"),
    ("deleted", "trash"),
    ("trash", "trash"),
    ("junk", "junk"),
    ("spam", "junk"),
    ("archive", "archive"),
)


def _folder_aad(folder_id: Any) -> str:
    return account_aad(folder_id, FOLDER_DELTA_FIELD)


def folders_delta_aad(account_id: Any) -> str:
    return account_aad(account_id, FOLDERS_DELTA_FIELD)


def _iso(value: str | None) -> datetime | None:
    """`2026-01-02T03:04:05Z` -> UTC datetime; malformed values return None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        log.info("graph_datetime_unparsed", value=value[:40])
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _addr_list(recipients: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in recipients or ():
        email = (item or {}).get("emailAddress") or {}
        addr = (email.get("address") or "").strip().lower()
        if not addr:
            continue
        out.append({"name": email.get("name") or "", "addr": addr})
    return out


def folder_role(item: dict[str, Any]) -> str:
    """Map a Graph folder to one of the schema's allowed roles."""
    well_known = (item.get("wellKnownName") or "").lower()
    if well_known in WELL_KNOWN_ROLES:
        return WELL_KNOWN_ROLES[well_known]
    name = (item.get("displayName") or "").lower()
    for needle, role in NAME_ROLES:
        if needle in name:
            return role
    return "other"


def record_from_item(folder_id: Any, item: dict[str, Any]) -> store.Record:
    """Build a `Record` from a single Graph delta item."""
    item_id = item.get("id") or ""
    message_id = item.get("internetMessageId")
    from_addrs = _addr_list([item.get("from")])
    flag = item.get("flag") or {}

    return store.Record(
        uid=None,
        remote_item_id=item_id,
        dedup_hash=(
            store.hash_message_id(message_id)
            if message_id
            else store.provisional_hash(folder_id, item_id)
        ),
        provisional=message_id is None,
        message_id=message_id,
        subject=item.get("subject"),
        subject_norm=normalize_subject(item.get("subject")),
        from_name=(from_addrs[0]["name"] if from_addrs else None) or None,
        from_addr=(from_addrs[0]["addr"] if from_addrs else None) or None,
        to_addrs=_addr_list(item.get("toRecipients")),
        cc_addrs=_addr_list(item.get("ccRecipients")),
        bcc_addrs=_addr_list(item.get("bccRecipients")),
        reply_to=_addr_list(item.get("replyTo")),
        internal_date=_iso(item.get("receivedDateTime")),
        snippet=(item.get("bodyPreview") or "")[:1024] or None,
        has_attachments=bool(item.get("hasAttachments")),
        flags={
            "flag_seen": bool(item.get("isRead")),
            "flag_flagged": (flag.get("flagStatus") or "").lower() == "flagged",
            "flag_draft": bool(item.get("isDraft")),
            "flag_answered": False,
            "flag_deleted": False,
        },
    )


def _record_failed_run(
    account_id: Any, folder_id: Any, kind: str, started_at: datetime, error: str
) -> SyncRun:
    return SyncRun(
        account_id=account_id,
        folder_id=folder_id,
        kind=kind,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        error=error[:2000],
    )


def is_delta_expired(exc: Exception) -> bool:
    from ..providers.graph import GraphRequestError

    if not isinstance(exc, GraphRequestError):
        return False
    return exc.status_code == 410 or (exc.code or "") in DELTA_EXPIRED_CODES


async def _folders_delta_link(session: AsyncSession, account_id: Any) -> str | None:
    encrypted = (
        await session.execute(
            select(GraphSettings.folders_delta_link_enc).where(
                GraphSettings.account_id == account_id
            )
        )
    ).scalar_one_or_none()
    if not encrypted:
        return None
    return decrypt(folders_delta_aad(account_id), encrypted).decode()


async def _store_folders_delta_link(
    session: AsyncSession, account_id: Any, link: str | None
) -> None:
    if not link:
        return
    settings_row = await session.get(GraphSettings, account_id)
    if settings_row is None:
        settings_row = GraphSettings(account_id=account_id)
        session.add(settings_row)
    settings_row.folders_delta_link_enc = encrypt(folders_delta_aad(account_id), link.encode())


async def sync_folders(session: AsyncSession, account: Account, client: Any) -> list[Folder]:
    """Sync the folder list (including hierarchy) from the Graph folders delta."""
    account_id = account.id
    link = await _folders_delta_link(session, account_id)
    try:
        items, delta_link = await client.folders_delta(link)
    except Exception as exc:
        if not is_delta_expired(exc):
            raise
        log.info("graph_folders_delta_expired", account_id=str(account_id))
        items, delta_link = await client.folders_delta(None)

    existing = {
        folder.remote_id: folder
        for folder in (
            await session.execute(select(Folder).where(Folder.account_id == account_id))
        ).scalars()
    }
    touched: list[Folder] = []
    for item in items:
        remote_id = item.get("id")
        if not remote_id or item.get("@removed"):
            continue
        name = item.get("displayName") or remote_id
        folder = existing.get(remote_id)
        if folder is None:
            folder = Folder(
                account_id=account_id,
                remote_id=remote_id,
                name=name,
                path=name,
                role=folder_role(item),
            )
            session.add(folder)
            existing[remote_id] = folder
        else:
            folder.name = name
            folder.path = name
            folder.role = folder_role(item)
        touched.append(folder)

    await session.flush()

    # Hierarchy: parent IDs are only known once every folder has a row.
    for item in items:
        remote_id = item.get("id")
        if not remote_id or item.get("@removed"):
            continue
        parent_remote_id = item.get("parentFolderId")
        folder = existing.get(remote_id)
        if folder is None:
            continue
        parent = existing.get(parent_remote_id) if parent_remote_id else None
        folder.parent_id = parent.id if parent is not None else None

    await _store_folders_delta_link(session, account_id, delta_link)
    await session.commit()
    return touched


async def sync_folder(
    session: AsyncSession,
    account: Account,
    folder: Folder,
    client: Any,
    *,
    force_full: bool = False,
) -> SyncStats:
    """One delta cycle for a single Graph folder."""
    folder_id = folder.id
    account_id = account.id
    owner_id = account.owner_id
    encrypted = None if force_full else folder.delta_link_enc
    link = decrypt(_folder_aad(folder_id), encrypted).decode() if encrypted else None
    full = link is None
    started_at = datetime.now(UTC)

    run = SyncRun(
        account_id=account_id,
        folder_id=folder_id,
        kind="full" if full else "incremental",
        started_at=started_at,
    )
    session.add(run)
    await session.flush()
    stats = SyncStats(kind=run.kind, full_resync=full)

    try:
        try:
            items, delta_link = await client.messages_delta(folder.remote_id, link)
        except Exception as exc:
            if link is None or not is_delta_expired(exc):
                raise
            log.info("graph_delta_expired", folder_id=str(folder_id))
            full = True
            run.kind = "full"
            stats.kind = "full"
            stats.full_resync = True
            items, delta_link = await client.messages_delta(folder.remote_id, None)

        removed_ids = [item["id"] for item in items if item.get("@removed") and item.get("id")]
        changed = [item for item in items if not item.get("@removed") and item.get("id")]

        if changed:
            records = [record_from_item(folder_id, item) for item in changed]
            known_ids = set(
                (
                    await session.execute(
                        select(MailboxMessage.remote_item_id).where(
                            MailboxMessage.folder_id == folder_id,
                            MailboxMessage.remote_item_id.in_(
                                [record.remote_item_id for record in records]
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            message_ids = await store.upsert_messages(session, account_id, records)
            await store.upsert_placements(session, folder_id, records, message_ids)
            stats.added = sum(1 for record in records if record.remote_item_id not in known_ids)
            stats.updated = len(records) - stats.added
            await store.upsert_search_rows(session, message_ids)
            for record in records:
                message_row_id = message_ids.get(record.dedup_hash)
                if message_row_id is not None:
                    await store.assign_subject_thread(session, account_id, message_row_id, record)

        if full:
            # A full delta enumerates the folder's entire contents, so anything not
            # mentioned no longer exists there (including `@removed` entries).
            stats.removed = await store.delete_missing_placements(
                session,
                account_id,
                folder_id,
                server_item_ids={item["id"] for item in changed},
            )
        elif removed_ids:
            stats.removed = await store.remove_placements(
                session, account_id, folder_id, removed_ids
            )

        folder.delta_link_enc = (
            encrypt(_folder_aad(folder_id), delta_link.encode()) if delta_link else None
        )
        folder.sync_pass = (folder.sync_pass or 0) + 1
        folder.last_synced_at = datetime.now(UTC)
        folder.sync_state = "idle"
        folder.sync_error = None

        stats.body_candidates = (
            await session.execute(
                select(func.count(Message.id))
                .join(MailboxMessage, MailboxMessage.message_id == Message.id)
                .where(MailboxMessage.folder_id == folder_id, Message.body_state == "none")
            )
        ).scalar_one()

        run.finished_at = datetime.now(UTC)
        run.added, run.updated, run.removed = stats.added, stats.updated, stats.removed
        await session.commit()
    except Exception as exc:
        await session.rollback()
        detail = f"{type(exc).__name__}: {exc}"
        await mark_folder_error(session, folder_id, detail)
        session.add(_record_failed_run(account_id, folder_id, stats.kind, started_at, detail))
        await session.commit()
        log.exception("graph_sync_folder_failed", folder_id=str(folder_id))
        raise

    await publish(
        owner_id,
        "sync.progress",
        {
            "account_id": str(account_id),
            "folder_id": str(folder_id),
            "kind": stats.kind,
            "added": stats.added,
            "updated": stats.updated,
            "removed": stats.removed,
        },
    )
    if stats.added and stats.kind == "incremental":
        # "New mail" signal for the GUI; the initial (`full`) sync does not trigger it.
        await publish(
            owner_id,
            "message.new",
            {"account_id": str(account_id), "folder_id": str(folder_id), "added": stats.added},
        )
    if stats.added or stats.updated or stats.removed:
        await publish(
            owner_id,
            "folder.counts",
            {"account_id": str(account_id), "folder_id": str(folder_id)},
        )
    return stats


def _partial_from_json(
    payload: dict[str, Any], attachments: Iterable[dict[str, Any]]
) -> ParsedMessage:
    """A message whose `$value` was refused by Graph: assemble content from the message JSON."""
    html = ((payload.get("body") or {}).get("content") or "").strip()
    content_type = ((payload.get("body") or {}).get("contentType") or "").lower()
    body_html = sanitize_html(html) if html and content_type == "html" else None
    body_text = (html_to_text(html) if html and content_type == "html" else html) or None
    snippet = (payload.get("bodyPreview") or "").strip() or None
    from_addrs = _addr_list([payload.get("from")])

    return ParsedMessage(
        dedup_hash=b"",
        size_bytes=None,
        rfc822_message_id=payload.get("internetMessageId"),
        subject=payload.get("subject"),
        subject_norm=normalize_subject(payload.get("subject")),
        from_name=(from_addrs[0]["name"] if from_addrs else None) or None,
        from_addr=(from_addrs[0]["addr"] if from_addrs else None) or None,
        to_addrs=_addr_list(payload.get("toRecipients")),
        cc_addrs=_addr_list(payload.get("ccRecipients")),
        bcc_addrs=_addr_list(payload.get("bccRecipients")),
        reply_to=_addr_list(payload.get("replyTo")),
        sent_date=_iso(payload.get("sentDateTime")) or _iso(payload.get("receivedDateTime")),
        has_attachments=bool(attachments),
        snippet=snippet[:1024] if snippet else None,
        body_text=body_text,
        body_html=body_html,
        attachments=[
            ParsedAttachment(
                part_path=item.get("id") or "",
                filename=item.get("name") or "attachment",
                mime_type=item.get("contentType"),
                size_bytes=item.get("size"),
                content_id=None,
                is_inline=bool(item.get("isInline")),
            )
            for item in attachments
        ],
        headers={"partial": "graph"},
    )


async def store_partial_message(
    session: AsyncSession,
    account: Account,
    message_row_id: Any,
    remote_item_id: str,
    client: Any,
) -> bool:
    """Fetch metadata + attachment list via JSON, mark the message `partial`."""
    payload = await client.message(
        remote_item_id,
        html_body=True,
        select=(
            "id,internetMessageId,subject,from,toRecipients,ccRecipients,bccRecipients,replyTo,"
            "sentDateTime,receivedDateTime,body,bodyPreview,hasAttachments,isRead,isDraft,flag"
        ),
    )
    attachments = (
        await client.message_attachments(remote_item_id) if payload.get("hasAttachments") else []
    )
    parsed = _partial_from_json(payload, attachments)
    message_row_id = await store.write_content(
        session, message_row_id, parsed, dedup_hash=None, body_state="partial"
    )
    await store.attach_thread(session, account.id, message_row_id, parsed)
    return True


async def fetch_bodies(
    session: AsyncSession,
    account: Account,
    folder: Folder,
    client: Any,
    *,
    limit: int | None = None,
) -> int:
    """Download bodies for this folder's messages that don't have one yet, newest first.

    Several `$value` requests run concurrently (bounded by `graph_body_concurrency`)
    since per-message latency dwarfs the CPU cost.
    """
    folder_id = folder.id
    account_id = account.id
    budget = settings.body_backfill_max_per_run if limit is None else limit

    rows = (
        await session.execute(
            select(Message.id, MailboxMessage.remote_item_id)
            .join(MailboxMessage, MailboxMessage.message_id == Message.id)
            .where(
                MailboxMessage.folder_id == folder_id,
                MailboxMessage.remote_item_id.is_not(None),
                Message.body_state == "none",
            )
            .order_by(Message.internal_date.desc().nulls_last())
            .limit(budget)
        )
    ).all()
    if not rows:
        return 0

    semaphore = asyncio.Semaphore(max(settings.graph_body_concurrency, 1))
    owner_id = account.owner_id
    stored = 0

    async def download(remote_item_id: str) -> tuple[str, bytes | None]:
        """`None` means Graph refused `$value` for this message (used by the fallback path)."""
        async with semaphore:
            try:
                return remote_item_id, await client.message_mime(remote_item_id)
            except Exception as exc:
                if is_body_refused(exc):
                    return remote_item_id, None
                raise

    for start in range(0, len(rows), settings.graph_body_concurrency):
        chunk = rows[start : start + settings.graph_body_concurrency]
        results = await asyncio.gather(
            *(download(row.remote_item_id) for row in chunk), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
            remote_item_id, raw = result
            row = next(row for row in chunk if row.remote_item_id == remote_item_id)
            if raw is None:
                log.info("graph_body_fallback", item_id=remote_item_id[:24])
                await store_partial_message(session, account, row.id, remote_item_id, client)
                await session.commit()
                stored += 1
                continue
            parsed = parse_message(raw)
            digest = await blobs.put(raw)
            message_row_id = await store.write_content(
                session, row.id, parsed, digest=digest, dedup_hash=parsed.dedup_hash
            )
            await store.attach_thread(session, account_id, message_row_id, parsed)
            await session.commit()
            stored += 1

    if stored:
        await publish(
            owner_id,
            "message.updated",
            {"account_id": str(account_id), "folder_id": str(folder_id), "count": stored},
        )
    return stored


def is_body_refused(exc: Exception | None) -> bool:
    """Graph refused `$value` for some messages (e.g. size/ID invalid)."""
    from ..providers.graph import GraphRequestError

    if not isinstance(exc, GraphRequestError):
        return False
    if exc.status_code in (400, 404, 413):
        return True
    return (exc.code or "") in BODY_FALLBACK_CODES
