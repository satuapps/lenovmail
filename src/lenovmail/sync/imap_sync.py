# Lenovmail — authored by satuapps
"""IMAP sync engine: incremental header pass, flags, deletion detection, and body backfill.

Ordering that determines correctness (do not change without reason):

1. `UIDVALIDITY` is compared first. If it changed, the folder's entire `mailbox_messages`
   is deleted and the cycle runs as a *full sync* — old UIDs are no longer meaningful.
2. New UIDs are searched with `UID <local_uidnext>:*` (incremental) or `ALL` (full).
3. Flag changes: `CHANGEDSINCE <highestmodseq>` if the server has CONDSTORE; without it
   flags for the whole folder are only refreshed every `FLAG_REFRESH_EVERY` cycles.
4. Deletion detection (full sync / flag refresh cycle) compares the server's UID set
   against `mailbox_messages`, then cleans up orphaned `messages`.
5. Bodies are fetched in a separate job (`fetch_bodies`) so the message list is already
   usable well before thousands of bodies finish downloading.

Writes to the database live in `sync/store.py` (shared with the Graph path). After a
rollback, never read attributes off ORM objects: the instance is stale and attribute
access triggers a synchronous lazy-load inside the event loop — that's why failure
status is written via `UPDATE`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from email.header import decode_header, make_header
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .. import blobs
from ..config import settings
from ..logging import get_logger
from ..models import Account, Folder, MailboxMessage, Message, SyncRun
from ..providers import imap_ops as ops
from ..providers.imap_pool import ImapConn, MailTransientError
from . import store
from .events import publish
from .normalize import normalize_subject, parse_message

log = get_logger(__name__)


@dataclass
class SyncStats:
    kind: str = "incremental"
    added: int = 0
    updated: int = 0
    removed: int = 0
    uidvalidity: int | None = None
    uidnext: int | None = None
    highestmodseq: int | None = None
    flag_refresh: bool = False
    full_resync: bool = False
    body_candidates: int = 0


# Old names are kept as aliases so external callers don't need to change.
Record = store.Record
provisional_hash = store.provisional_hash
hash_message_id = store.hash_message_id


# --- ENVELOPE value conversion ----------------------------------------------------


def _decode(value: Any) -> str | None:
    """Byte/str -> str. RFC 2047 encoded-words are decoded when possible."""
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text_value = str(value)
    if "=?" not in text_value:
        return text_value.strip() or None
    try:
        return str(make_header(decode_header(text_value))).strip() or None
    except (UnicodeDecodeError, ValueError, LookupError):
        log.info("encoded_word_decode_failed")
        return text_value.strip() or None


def _addr_list(envelope_addresses: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in envelope_addresses or ():
        mailbox = _decode(getattr(item, "mailbox", None))
        host = _decode(getattr(item, "host", None))
        if not mailbox:
            continue
        addr = f"{mailbox}@{host}".lower() if host else mailbox.lower()
        out.append({"name": _decode(getattr(item, "name", None)) or "", "addr": addr})
    return out


def record_from_fetch(folder_id: Any, uid: int, data: dict) -> store.Record:
    """Build a `Record` from a FETCH response entry (ENVELOPE + FLAGS + INTERNALDATE)."""
    envelope = data.get(b"ENVELOPE")
    message_id = _decode(getattr(envelope, "message_id", None)) if envelope else None
    subject = _decode(getattr(envelope, "subject", None)) if envelope else None
    from_addrs = _addr_list(getattr(envelope, "from_", None)) if envelope else []

    return store.Record(
        uid=uid,
        dedup_hash=(
            store.hash_message_id(message_id)
            if message_id
            else store.provisional_hash(folder_id, uid)
        ),
        provisional=message_id is None,
        message_id=message_id,
        subject=subject,
        subject_norm=normalize_subject(subject),
        from_name=(from_addrs[0]["name"] if from_addrs else None) or None,
        from_addr=(from_addrs[0]["addr"] if from_addrs else None) or None,
        to_addrs=_addr_list(getattr(envelope, "to", None)) if envelope else [],
        cc_addrs=_addr_list(getattr(envelope, "cc", None)) if envelope else [],
        bcc_addrs=_addr_list(getattr(envelope, "bcc", None)) if envelope else [],
        reply_to=_addr_list(getattr(envelope, "reply_to", None)) if envelope else [],
        internal_date=data.get(b"INTERNALDATE"),
        size_bytes=data.get(b"RFC822.SIZE"),
        flags=store.flags_to_columns(data.get(b"FLAGS") or []),
        modseq=ops.extract_modseq(data),
    )


def _record_failed_run(
    account_id: Any,
    folder_id: Any,
    kind: str,
    started_at: datetime,
    error: str,
) -> SyncRun:
    return SyncRun(
        account_id=account_id,
        folder_id=folder_id,
        kind=kind,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        error=error[:2000],
    )


async def mark_folder_error(session: AsyncSession, folder_id: Any, error: str) -> None:
    """Mark a folder sync-failed via UPDATE (safe after rollback)."""
    await session.execute(
        update(Folder)
        .where(Folder.id == folder_id)
        .values(sync_state="error", sync_error=error[:2000])
    )


async def sync_folders(session: AsyncSession, account: Account, conn: ImapConn) -> list[Folder]:
    """Reconcile the IMAP folder list into the `folders` table (upsert by path).

    Without this a newly created IMAP account has zero folder rows, so the
    `sync_account` cycle has nothing to sync. Role is taken from SPECIAL-USE
    when the server advertises it, otherwise from a name heuristic; the
    hierarchy is built from the delimiter reported by the server.
    """
    infos = await ops.list_folders(conn)
    existing = {
        folder.remote_id: folder
        for folder in (
            await session.execute(select(Folder).where(Folder.account_id == account.id))
        ).scalars()
    }

    for info in infos:
        folder = existing.get(info.name)
        if folder is None:
            folder = Folder(
                account_id=account.id,
                remote_id=info.name,
                name=info.name,
                path=info.name,
                role=info.role,
            )
            session.add(folder)
            existing[info.name] = folder
        else:
            folder.name = info.name
            folder.path = info.name
            folder.role = info.role

    await session.flush()

    for info in infos:
        folder = existing[info.name]
        delimiter = info.delimiter or ""
        parent_name = (
            info.name.rsplit(delimiter, 1)[0] if delimiter and delimiter in info.name else None
        )
        parent = existing.get(parent_name) if parent_name else None
        folder.parent_id = parent.id if parent is not None else None

    await session.commit()
    return [existing[info.name] for info in infos]


# --- main cycle ---------------------------------------------------------------


async def sync_folder(
    session: AsyncSession,
    account: Account,
    folder: Folder,
    conn: ImapConn,
    *,
    force_full: bool = False,
) -> SyncStats:
    """One sync cycle for a single folder. `conn` is borrowed from the caller's pool."""
    folder_id = folder.id
    account_id = account.id
    owner_id = account.owner_id
    local_uidvalidity = folder.uidvalidity
    has_local_state = local_uidvalidity is not None
    full = force_full or not has_local_state
    started_at = datetime.now(UTC)

    run = SyncRun(
        account_id=account_id,
        folder_id=folder_id,
        kind="full" if full else "incremental",
        started_at=started_at,
    )
    session.add(run)
    await session.flush()

    stats = SyncStats(kind=run.kind)
    try:
        info = await ops.select(conn, folder.path or folder.name)
        stats.uidvalidity = info.uidvalidity
        stats.uidnext = info.uidnext
        stats.highestmodseq = info.highestmodseq

        if info.uidvalidity is not None and local_uidvalidity != info.uidvalidity:
            if has_local_state:
                log.info(
                    "uidvalidity_changed",
                    folder_id=str(folder_id),
                    old=local_uidvalidity,
                    new=info.uidvalidity,
                )
            await session.execute(
                delete(MailboxMessage).where(MailboxMessage.folder_id == folder_id)
            )
            folder.highestmodseq = None
            folder.uidnext = None
            full = True
            run.kind = "full"
            stats.kind = "full"
            stats.full_resync = True

        # 1) New UIDs.
        all_uids = await ops.search_all_uids(conn)
        new_uids = (
            all_uids
            if full or not folder.uidnext
            else await ops.search_uids_since(conn, folder.uidnext)
        )

        # 2) Flag changes on already-known messages.
        refresh_flags = full or (folder.sync_pass % max(settings.flag_refresh_every, 1) == 0)
        if not full and conn.condstore and folder.highestmodseq and all_uids:
            changed = await ops.fetch_flags_changed_since(conn, folder.highestmodseq, all_uids)
            stats.updated += await store.apply_flags_by_uid(session, folder_id, changed)
        elif refresh_flags and all_uids:
            flags = await ops.fetch_all_flags(conn, all_uids)
            stats.updated += await store.apply_flags_by_uid(session, folder_id, flags)
            stats.flag_refresh = True

        # 3) Header pass for new UIDs.
        for start in range(0, len(new_uids), settings.header_fetch_chunk):
            chunk = new_uids[start : start + settings.header_fetch_chunk]
            response = await ops.fetch_headers(conn, chunk)
            records = [
                record_from_fetch(folder_id, int(uid), data) for uid, data in response.items()
            ]
            if not records:
                continue
            message_ids = await store.upsert_messages(session, account_id, records)
            stats.added += await store.upsert_placements(session, folder_id, records, message_ids)
            await store.upsert_search_rows(session, message_ids)
            for record in records:
                message_row_id = message_ids.get(record.dedup_hash)
                if message_row_id is None:
                    continue
                # Header reference isn't known yet; the thread is built from the
                # subject first and merged when the body arrives.
                await store.assign_subject_thread(session, account_id, message_row_id, record)

        # 4) Deletion detection.
        if full or refresh_flags:
            stats.removed = await store.delete_missing_placements(
                session, account_id, folder_id, server_uids=set(all_uids)
            )

        # 5) Folder cursor.
        folder.uidvalidity = info.uidvalidity
        folder.uidnext = info.uidnext
        folder.highestmodseq = info.highestmodseq
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
    except MailTransientError as exc:
        await session.rollback()
        await mark_folder_error(session, folder_id, str(exc))
        session.add(_record_failed_run(account_id, folder_id, stats.kind, started_at, str(exc)))
        await session.commit()
        raise
    except Exception as exc:
        await session.rollback()
        detail = f"{type(exc).__name__}: {exc}"
        await mark_folder_error(session, folder_id, detail)
        session.add(_record_failed_run(account_id, folder_id, stats.kind, started_at, detail))
        await session.commit()
        log.exception("sync_folder_failed", folder_id=str(folder_id))
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
        # "New mail" signal for the GUI; the first sync (`kind == "full"`) doesn't
        # trigger it so the initial batch of thousands of messages doesn't flood clients.
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


async def store_partial_headers(
    session: AsyncSession,
    account: Account,
    message_row_id: Any,
    uid: int,
    conn: ImapConn,
) -> bool:
    """Message too large: fetch header + first body part, mark it `partial`.

    `body_state='partial'` prevents this message from being re-fetched over and over;
    the message list and search stay populated from subject/sender.
    """
    header = await ops.fetch_header_only(conn, uid)
    if header is None:
        return False
    first_part = (await conn.fetch([uid], ["BODY.PEEK[1]"])).get(uid, {}).get(b"BODY[1]")
    parsed = parse_message(header + (first_part or b""))
    # Only partially parsed length: keep RFC822.SIZE from the header stage.
    parsed.size_bytes = None

    message_row_id = await store.write_content(
        session,
        message_row_id,
        parsed,
        dedup_hash=parsed.dedup_hash,
        body_state="partial",
    )
    await store.attach_thread(session, account.id, message_row_id, parsed)
    return True


async def fetch_bodies(
    session: AsyncSession,
    account: Account,
    folder: Folder,
    conn: ImapConn,
    *,
    limit: int | None = None,
) -> int:
    """Download + parse bodies for messages that don't have one yet (`body_state='none'`),
    newest first.

    The folder is selected here: a pooled connection can be used at any time while in
    AUTH state, and `UID FETCH` is only valid after the folder has been SELECTed.
    """
    folder_id = folder.id
    account_id = account.id
    owner_id = account.owner_id
    budget = settings.body_backfill_max_per_run if limit is None else limit
    await ops.select(conn, folder.path or folder.name)

    rows = (
        await session.execute(
            select(Message.id, MailboxMessage.remote_uid, Message.size_bytes)
            .join(MailboxMessage, MailboxMessage.message_id == Message.id)
            .where(
                MailboxMessage.folder_id == folder_id,
                MailboxMessage.remote_uid.is_not(None),
                Message.body_state == "none",
            )
            .order_by(Message.internal_date.desc().nulls_last())
            .limit(budget)
        )
    ).all()
    if not rows:
        return 0

    max_bytes = settings.body_full_fetch_max_bytes
    small = [row for row in rows if (row.size_bytes or 0) <= max_bytes]
    large = [row for row in rows if (row.size_bytes or 0) > max_bytes]
    stored = 0

    for start in range(0, len(small), settings.body_fetch_chunk):
        chunk = small[start : start + settings.body_fetch_chunk]
        by_uid = {int(row.remote_uid): row for row in chunk}
        raw_map = await ops.fetch_raw(conn, list(by_uid))
        for uid, raw in raw_map.items():
            row = by_uid[uid]
            parsed = parse_message(raw)
            digest = await blobs.put(raw)
            message_row_id = await store.write_content(
                session, row.id, parsed, digest=digest, dedup_hash=parsed.dedup_hash
            )
            await store.attach_thread(session, account_id, message_row_id, parsed)
            stored += 1
        await session.commit()

    for row in large:
        uid = int(row.remote_uid)
        log.info("fetching_partial_body", uid=uid, size=row.size_bytes)
        if await store_partial_headers(session, account, row.id, uid, conn):
            stored += 1
        else:
            await session.execute(
                update(Message).where(Message.id == row.id).values(body_state="partial")
            )
        await session.commit()

    if stored:
        await publish(
            owner_id,
            "message.updated",
            {"account_id": str(account_id), "folder_id": str(folder_id), "count": stored},
        )
    return stored
