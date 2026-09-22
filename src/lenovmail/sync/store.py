# Lenovmail — authored by satuapps (satuapps.com)
"""Mail data writer for Postgres, shared by the IMAP and Microsoft Graph sync paths.

Both providers produce the same `Record` (message metadata) and `ParsedMessage` (full
MIME parse result), then this layer handles message identity (`dedup_hash`), per-folder
placement, the search index, attachments, and threading. Keeping it in one module avoids
forking dedup/upsert logic across providers.

This module does **not** commit; the caller owns the transaction.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    Attachment,
    MailboxMessage,
    Message,
    MessageBody,
    MessageRef,
    MessageSearch,
    MessageValue,
)
from ..search import tsvector_expr
from .mining import mine_values
from .normalize import ParsedMessage, search_tsvector_input
from .threads import assign_thread

# Flag columns on `mailbox_messages` (order is used to group UPDATEs).
FLAG_COLUMN_NAMES: tuple[str, ...] = (
    "flag_seen",
    "flag_flagged",
    "flag_answered",
    "flag_draft",
    "flag_deleted",
)

# IMAP flag name -> column. Graph maps `isRead`/`flag.flagStatus`/`isDraft` to the same
# columns, so this mapping is shared by both paths.
IMAP_FLAG_TO_COLUMN: dict[str, str] = {
    "\\SEEN": "flag_seen",
    "\\FLAGGED": "flag_flagged",
    "\\ANSWERED": "flag_answered",
    "\\DRAFT": "flag_draft",
    "\\DELETED": "flag_deleted",
}


def flags_to_columns(flags: Iterable[str]) -> dict[str, bool]:
    """IMAP flag names -> boolean columns. Unknown names are ignored."""
    upper = {flag.upper() for flag in flags}
    return {column: name in upper for name, column in IMAP_FLAG_TO_COLUMN.items()}


@dataclass
class Record:
    """Metadata for one message from the header stage (no body yet)."""

    dedup_hash: bytes
    provisional: bool
    uid: int | None = None
    remote_item_id: str | None = None
    message_id: str | None = None
    subject: str | None = None
    subject_norm: str | None = None
    from_name: str | None = None
    from_addr: str | None = None
    to_addrs: list[dict[str, str]] = field(default_factory=list)
    cc_addrs: list[dict[str, str]] = field(default_factory=list)
    bcc_addrs: list[dict[str, str]] = field(default_factory=list)
    reply_to: list[dict[str, str]] = field(default_factory=list)
    internal_date: datetime | None = None
    size_bytes: int | None = None
    snippet: str | None = None
    has_attachments: bool = False
    flags: dict[str, bool] = field(default_factory=dict)
    modseq: int | None = None


def hash_message_id(message_id: str) -> bytes:
    return hashlib.sha256(message_id.encode("utf-8", "replace")).digest()


def provisional_hash(folder_id: uuid.UUID, handle: object) -> bytes:
    """Provisional hash for a message that has no Message-ID yet.

    Replaced with the final hash (`sha256(raw MIME)`) once the body is fetched, via
    `ensure_dedup_hash`.
    """
    return hashlib.sha256(f"{folder_id}:{handle}".encode()).digest()


async def upsert_messages(
    session: AsyncSession, account_id: uuid.UUID, records: Sequence[Record]
) -> dict[bytes, uuid.UUID]:
    """Map `dedup_hash` -> `messages` id, creating rows that don't exist yet.

    The same message can appear in multiple folders (e.g. `All Mail` + `Archive`); its
    identity is one `messages` row, its placements are many `mailbox_messages` rows.
    """
    if not records:
        return {}
    hashes = list(dict.fromkeys(record.dedup_hash for record in records))

    existing: dict[bytes, uuid.UUID] = {
        row[0]: row[1]
        for row in (
            await session.execute(
                select(Message.dedup_hash, Message.id).where(
                    Message.account_id == account_id, Message.dedup_hash.in_(hashes)
                )
            )
        ).all()
    }

    payload = [
        {
            "account_id": account_id,
            "dedup_hash": record.dedup_hash,
            "rfc822_message_id": record.message_id,
            "subject": record.subject,
            "subject_norm": record.subject_norm,
            "from_name": record.from_name,
            "from_addr": record.from_addr,
            "to_addrs": record.to_addrs or None,
            "cc_addrs": record.cc_addrs or None,
            "bcc_addrs": record.bcc_addrs or None,
            "reply_to": record.reply_to or None,
            "internal_date": record.internal_date,
            "size_bytes": record.size_bytes,
            "snippet": record.snippet,
            "has_attachments": record.has_attachments,
            "body_state": "none",
            # Marker that `dedup_hash` is still provisional (Message-ID not known yet).
            "headers": {"provisional": True} if record.provisional else None,
        }
        for record in records
        if record.dedup_hash not in existing
    ]
    if payload:
        inserted = await session.execute(
            pg_insert(Message)
            .values(payload)
            .on_conflict_do_nothing(index_elements=["account_id", "dedup_hash"])
            .returning(Message.dedup_hash, Message.id)
        )
        for digest, message_row_id in inserted.all():
            existing[digest] = message_row_id

        missing = [digest for digest in hashes if digest not in existing]
        if missing:
            for row in (
                await session.execute(
                    select(Message.dedup_hash, Message.id).where(
                        Message.account_id == account_id, Message.dedup_hash.in_(missing)
                    )
                )
            ).all():
                existing[row[0]] = row[1]
    return existing


def _placement_payload(
    folder_id: uuid.UUID,
    record: Record,
    message_id: uuid.UUID,
    now: datetime,
) -> dict[str, Any]:
    return {
        "folder_id": folder_id,
        "message_id": message_id,
        "remote_uid": record.uid,
        "remote_item_id": record.remote_item_id,
        "modseq": record.modseq,
        "last_seen_at": now,
        **record.flags,
    }


async def upsert_placements(
    session: AsyncSession,
    folder_id: uuid.UUID,
    records: Sequence[Record],
    message_ids: dict[bytes, uuid.UUID],
) -> int:
    """Write/update `mailbox_messages` for a batch of records."""
    now = datetime.now(UTC)
    kept = [record for record in records if record.dedup_hash in message_ids]
    if not kept:
        return 0

    # Two partial unique indexes (IMAP UID, Graph item) can't both be used in one
    # statement: `ON CONFLICT` only names one index. NULL rows never enter a partial
    # index, so records must be split — otherwise the Graph path writes duplicate rows.
    uid_rows: list[dict[str, Any]] = []
    item_rows: list[dict[str, Any]] = []
    for record in kept:
        payload = _placement_payload(folder_id, record, message_ids[record.dedup_hash], now)
        if record.remote_item_id is not None:
            item_rows.append(payload)
        elif record.uid is not None:
            uid_rows.append(payload)

    if uid_rows:
        statement = pg_insert(MailboxMessage).values(uid_rows)
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=["folder_id", "remote_uid"],
                index_where=text("remote_uid is not null"),
                set_={
                    "message_id": statement.excluded.message_id,
                    "remote_item_id": statement.excluded.remote_item_id,
                    "modseq": statement.excluded.modseq,
                    "last_seen_at": statement.excluded.last_seen_at,
                    **{column: getattr(statement.excluded, column) for column in FLAG_COLUMN_NAMES},
                },
            )
        )
    if item_rows:
        item_statement = pg_insert(MailboxMessage).values(item_rows)
        await session.execute(
            item_statement.on_conflict_do_update(
                index_elements=["folder_id", "remote_item_id"],
                index_where=text("remote_item_id is not null"),
                set_={
                    "message_id": item_statement.excluded.message_id,
                    "remote_uid": item_statement.excluded.remote_uid,
                    "modseq": item_statement.excluded.modseq,
                    "last_seen_at": item_statement.excluded.last_seen_at,
                    **{
                        column: getattr(item_statement.excluded, column)
                        for column in FLAG_COLUMN_NAMES
                    },
                },
            )
        )
    return len(kept)


async def upsert_search_rows(session: AsyncSession, message_ids: dict[bytes, uuid.UUID]) -> None:
    """Populate `message_search` from subject/sender (full text added once the body arrives)."""
    if not message_ids:
        return
    rows = (
        await session.execute(
            select(Message.id, Message.subject, Message.from_name, Message.from_addr).where(
                Message.id.in_(list(dict.fromkeys(message_ids.values())))
            )
        )
    ).all()
    payload = [
        {
            "message_id": row.id,
            "tsv": tsvector_expr(
                search_tsvector_input(row.subject, row.from_name, row.from_addr, None)
            ),
        }
        for row in rows
    ]
    if not payload:
        return
    statement = pg_insert(MessageSearch).values(payload)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["message_id"], set_={"tsv": statement.excluded.tsv}
        )
    )


async def execute_count(session: AsyncSession, statement: Any) -> int:
    """Run a DML statement and return the number of affected rows.

    `AsyncSession.execute` is stubbed as `Result[Any]` so `rowcount` is invisible to the
    type checker; the loose access is collected here in one place.
    """
    result = await session.execute(statement)
    return int(getattr(result, "rowcount", 0) or 0)


async def _update_flags_grouped(
    session: AsyncSession,
    filter_clause: Callable[[list[Any]], Sequence[Any]],
    flag_sets: dict[tuple[bool, ...], list[Any]],
) -> int:
    """Apply flags per combination group, one UPDATE per group."""
    updated = 0
    for key, handles in flag_sets.items():
        values = dict(zip(FLAG_COLUMN_NAMES, key, strict=True))
        updated += await execute_count(
            session,
            update(MailboxMessage).where(*filter_clause(handles)).values(**values),
        )
    return updated


def _group_flags(
    flags_by_handle: Mapping[Any, Iterable[str]],
) -> dict[tuple[bool, ...], list[Any]]:
    grouped: dict[tuple[bool, ...], list[Any]] = {}
    for handle, flags in flags_by_handle.items():
        columns = flags_to_columns(flags)
        key = tuple(columns[name] for name in FLAG_COLUMN_NAMES)
        grouped.setdefault(key, []).append(handle)
    return grouped


async def apply_flags_by_uid(
    session: AsyncSession, folder_id: uuid.UUID, changed: Mapping[int, Iterable[str]]
) -> int:
    """Apply flags from a FETCH result to placements by UID (IMAP path)."""
    if not changed:
        return 0
    grouped = _group_flags(changed)
    return await _update_flags_grouped(
        session,
        lambda uids: (
            MailboxMessage.folder_id == folder_id,
            MailboxMessage.remote_uid.in_(uids),
        ),
        grouped,
    )


async def apply_flags_by_message_id(
    session: AsyncSession, flags_by_message_id: Mapping[uuid.UUID, Iterable[str]]
) -> int:
    """Apply flags to all placements of a message (Graph path, ids are per-message)."""
    if not flags_by_message_id:
        return 0
    grouped = _group_flags(flags_by_message_id)
    return await _update_flags_grouped(
        session,
        lambda message_ids: (MailboxMessage.message_id.in_(message_ids),),
        grouped,
    )


async def set_flag_columns(
    session: AsyncSession, message_id: uuid.UUID, values: Mapping[str, bool]
) -> int:
    """Update a subset of flag columns on all placements of one message.

    Used by GUI/agent actions that change a single flag (e.g. `\\Seen`); unlike
    `apply_flags_by_message_id`, which overwrites every column from the server's flag list.
    """
    if not values:
        return 0
    unknown = set(values) - set(FLAG_COLUMN_NAMES)
    if unknown:
        raise ValueError(f"unknown flag column(s): {sorted(unknown)}")
    return await execute_count(
        session,
        update(MailboxMessage).where(MailboxMessage.message_id == message_id).values(**values),
    )


async def purge_orphan_messages(session: AsyncSession, account_id: uuid.UUID) -> None:
    """Delete `messages` that no longer have a placement in any folder of the account."""
    await session.execute(
        text(
            "DELETE FROM messages m WHERE m.account_id = :account_id "
            "AND NOT EXISTS (SELECT 1 FROM mailbox_messages mm WHERE mm.message_id = m.id)"
        ),
        {"account_id": account_id},
    )


async def remove_placements(
    session: AsyncSession,
    account_id: uuid.UUID,
    folder_id: uuid.UUID,
    item_ids: Iterable[str],
) -> int:
    """Delete specific placements, then clean up orphaned messages.

    Used by Graph delta, which reports removed items one at a time — distinguishing "not
    mentioned in the delta" (still present) from "reported `@removed`" (now gone).
    """
    ids = sorted({item_id for item_id in item_ids if item_id})
    if not ids:
        return 0
    removed = await execute_count(
        session,
        delete(MailboxMessage).where(
            MailboxMessage.folder_id == folder_id,
            MailboxMessage.remote_item_id.in_(ids),
        ),
    )
    await purge_orphan_messages(session, account_id)
    return removed


async def remove_placements_by_uid(
    session: AsyncSession,
    account_id: uuid.UUID,
    folder_id: uuid.UUID,
    uids: Iterable[int],
) -> int:
    """Delete placements by UID (IMAP path), then clean up orphaned messages.

    Used by move/delete actions: the server has already done it, so local state is
    tidied up immediately without waiting for the next flag-refresh cycle.
    """
    ids = sorted(set(uids))
    if not ids:
        return 0
    removed = await execute_count(
        session,
        delete(MailboxMessage).where(
            MailboxMessage.folder_id == folder_id,
            MailboxMessage.remote_uid.in_(ids),
        ),
    )
    await purge_orphan_messages(session, account_id)
    return removed


async def delete_missing_placements(
    session: AsyncSession,
    account_id: uuid.UUID,
    folder_id: uuid.UUID,
    *,
    server_uids: set[int] | None = None,
    server_item_ids: set[str] | None = None,
) -> int:
    """Delete placements missing from the server, then clean up orphaned messages."""
    removed = 0
    if server_uids is not None:
        local = {
            uid
            for uid in (
                await session.execute(
                    select(MailboxMessage.remote_uid).where(
                        MailboxMessage.folder_id == folder_id,
                        MailboxMessage.remote_uid.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
            if uid is not None
        }
        gone = sorted(local - server_uids)
        if gone:
            removed += await execute_count(
                session,
                delete(MailboxMessage).where(
                    MailboxMessage.folder_id == folder_id,
                    MailboxMessage.remote_uid.in_(gone),
                ),
            )

    if server_item_ids is not None:
        local_items = {
            item_id
            for item_id in (
                await session.execute(
                    select(MailboxMessage.remote_item_id).where(
                        MailboxMessage.folder_id == folder_id,
                        MailboxMessage.remote_item_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
            if item_id is not None
        }
        gone_items = sorted(local_items - server_item_ids)
        if gone_items:
            removed += await execute_count(
                session,
                delete(MailboxMessage).where(
                    MailboxMessage.folder_id == folder_id,
                    MailboxMessage.remote_item_id.in_(gone_items),
                ),
            )

    await purge_orphan_messages(session, account_id)
    return removed


async def ensure_dedup_hash(
    session: AsyncSession, message_row_id: uuid.UUID, desired: bytes
) -> uuid.UUID:
    """Set the final `dedup_hash`, merging into an existing twin row if one exists.

    Used when the body is fetched: a provisional hash (`folder:handle`) or a Message-ID
    hash is replaced with `sha256(raw MIME)`. If another row already uses that hash, its
    placements are moved over and the old row is deleted.
    """
    current = (
        await session.execute(select(Message.dedup_hash).where(Message.id == message_row_id))
    ).scalar_one_or_none()
    if current == desired:
        return message_row_id

    twin_id = (
        (
            await session.execute(
                select(Message.id).where(
                    Message.dedup_hash == desired, Message.id != message_row_id
                )
            )
        )
        .scalars()
        .first()
    )
    if twin_id is not None:
        await session.execute(
            update(MailboxMessage)
            .where(MailboxMessage.message_id == message_row_id)
            .values(message_id=twin_id)
        )
        await session.execute(delete(Message).where(Message.id == message_row_id))
        return twin_id

    await session.execute(
        update(Message).where(Message.id == message_row_id).values(dedup_hash=desired)
    )
    return message_row_id


async def append_refs(
    session: AsyncSession, message_row_id: uuid.UUID, refs: Sequence[str]
) -> None:
    """Append `Message-ID` header references (idempotent: existing refs are skipped)."""
    if not refs:
        return
    existing = set(
        (
            await session.execute(
                select(MessageRef.ref).where(MessageRef.message_id == message_row_id)
            )
        )
        .scalars()
        .all()
    )
    position = (
        await session.execute(
            select(func.coalesce(func.max(MessageRef.position), -1) + 1).where(
                MessageRef.message_id == message_row_id
            )
        )
    ).scalar_one()
    for ref in refs:
        if not ref or ref in existing:
            continue
        session.add(MessageRef(message_id=message_row_id, position=position, ref=ref))
        existing.add(ref)
        position += 1


async def write_content(
    session: AsyncSession,
    message_row_id: uuid.UUID,
    parsed: ParsedMessage,
    *,
    digest: bytes | None = None,
    dedup_hash: bytes | None = None,
    body_state: str = "full",
    internal_date: datetime | None = None,
) -> uuid.UUID:
    """Write final metadata, body, attachments, search index, and thread for one message.

    `dedup_hash=None` means the message identity is left as-is (used by paths that have
    no MIME bytes, e.g. the Graph JSON fallback).
    """
    if dedup_hash is not None:
        message_row_id = await ensure_dedup_hash(session, message_row_id, dedup_hash)

    values: dict[str, Any] = {
        "body_state": body_state,
        "has_attachments": parsed.has_attachments,
        "snippet": parsed.snippet,
        "subject": parsed.subject,
        "subject_norm": parsed.subject_norm,
        "from_addr": parsed.from_addr,
        "from_name": parsed.from_name,
        "to_addrs": parsed.to_addrs or None,
        "cc_addrs": parsed.cc_addrs or None,
        "bcc_addrs": parsed.bcc_addrs or None,
        "reply_to": parsed.reply_to or None,
        "sent_date": parsed.sent_date,
        "rfc822_message_id": parsed.rfc822_message_id,
        "headers": parsed.headers or None,
        "values_mined_at": datetime.now(UTC),
    }
    if parsed.size_bytes is not None:
        values["size_bytes"] = parsed.size_bytes
    if digest is not None:
        values["blob_sha256"] = digest
    if internal_date is not None:
        values["internal_date"] = internal_date
    await session.execute(update(Message).where(Message.id == message_row_id).values(**values))

    await session.execute(delete(MessageBody).where(MessageBody.message_id == message_row_id))
    session.add(
        MessageBody(
            message_id=message_row_id,
            body_text=parsed.body_text,
            body_html=parsed.body_html,
        )
    )

    # Re-mined from scratch on every body write: a message upgraded from `partial` to
    # `full` must not keep values extracted from the shorter text.
    await session.execute(delete(MessageValue).where(MessageValue.message_id == message_row_id))
    mined = mine_values(parsed.subject, parsed.body_text)
    if mined:
        session.add_all(
            [
                MessageValue(
                    message_id=message_row_id,
                    kind=item.kind,
                    value=item.value,
                    confidence=item.confidence,
                )
                for item in mined
            ]
        )

    await session.execute(delete(Attachment).where(Attachment.message_id == message_row_id))
    if parsed.attachments:
        session.add_all(
            [
                Attachment(
                    message_id=message_row_id,
                    part_path=item.part_path,
                    filename=item.filename,
                    mime_type=item.mime_type,
                    size_bytes=item.size_bytes,
                    content_id=item.content_id,
                    is_inline=item.is_inline,
                )
                for item in parsed.attachments
            ]
        )

    search_tsv = tsvector_expr(
        search_tsvector_input(parsed.subject, parsed.from_name, parsed.from_addr, parsed.body_text)
    )
    statement = pg_insert(MessageSearch).values(message_id=message_row_id, tsv=search_tsv)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["message_id"], set_={"tsv": statement.excluded.tsv}
        )
    )

    await append_refs(session, message_row_id, parsed.refs)
    return message_row_id


async def attach_thread(
    session: AsyncSession,
    account_id: uuid.UUID,
    message_row_id: uuid.UUID,
    parsed: ParsedMessage,
) -> None:
    """Create/merge a thread using header references from the parsed body."""
    await assign_thread(
        session,
        account_id=account_id,
        message_row_id=message_row_id,
        msg_id_header=parsed.rfc822_message_id,
        refs=parsed.refs,
        subject_norm=parsed.subject_norm,
        internal_date=parsed.sent_date,
    )


async def assign_subject_thread(
    session: AsyncSession,
    account_id: uuid.UUID,
    message_row_id: uuid.UUID,
    record: Record,
) -> None:
    """Initial thread for a message from the header stage (`References` refs not known yet).

    New references become available once the body is fetched; at that point
    `attach_thread` merges the threads that should be unified.
    """
    await assign_thread(
        session,
        account_id=account_id,
        message_row_id=message_row_id,
        msg_id_header=record.message_id,
        refs=[],
        subject_norm=record.subject_norm,
        internal_date=record.internal_date,
    )
