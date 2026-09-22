# Lenovmail — authored by satuapps (satuapps.com)
"""Data shaping for the API: folder counts, per-message flags, and page cursors.

Every query here is scoped by account/ownership so no path can read another account's
data — callers are still required to check ownership first.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import (
    Account,
    Attachment,
    Folder,
    MailboxMessage,
    Message,
    MessageBody,
    MessageValue,
    Thread,
)
from ..sync.mining import VALUE_KINDS
from .schemas import (
    AccountOut,
    AddressOut,
    AttachmentOut,
    BodyOut,
    FolderOut,
    MessageOut,
    ThreadOut,
)

# How long before the janitor retires an account the GUI starts warning about it.
RETIRE_WARN_DAYS = 3


async def folder_counts(
    session: AsyncSession, account_id: uuid.UUID
) -> dict[uuid.UUID, tuple[int, int]]:
    """`folder_id -> (unread, total)` for every folder in the account."""
    rows = (
        await session.execute(
            select(
                MailboxMessage.folder_id,
                func.count().label("total"),
                func.count().filter(~MailboxMessage.flag_seen).label("unread"),
            )
            .join(Folder, Folder.id == MailboxMessage.folder_id)
            .where(Folder.account_id == account_id)
            .group_by(MailboxMessage.folder_id)
        )
    ).all()
    return {row[0]: (row[2], row[1]) for row in rows}


async def folders_out(session: AsyncSession, account_id: uuid.UUID) -> list[FolderOut]:
    rows = (
        await session.execute(
            select(Folder).where(Folder.account_id == account_id).order_by(Folder.name)
        )
    ).scalars()
    counts = await folder_counts(session, account_id)
    out: list[FolderOut] = []
    for folder in rows:
        unread, total = counts.get(folder.id, (0, 0))
        item = FolderOut.model_validate(folder)
        item.unread, item.total = unread, total
        out.append(item)
    return out


async def accounts_out(session: AsyncSession, accounts: Sequence[Account]) -> list[AccountOut]:
    """Accounts + inbox counts (the headline numbers shown in the account list)."""
    out: list[AccountOut] = []
    for account in accounts:
        counts = (
            await session.execute(
                select(
                    func.count(MailboxMessage.id).label("total"),
                    func.count(MailboxMessage.id).filter(~MailboxMessage.flag_seen).label("unread"),
                )
                .select_from(Folder)
                .join(MailboxMessage, MailboxMessage.folder_id == Folder.id)
                .where(Folder.account_id == account.id, Folder.role == "inbox")
            )
        ).one()
        item = account_out(account)
        item.total, item.unread = counts[0], counts[1]
        out.append(item)
    return out


def account_out(account: Account) -> AccountOut:
    """One account row, including when the janitor would retire it.

    `maintenance.sweep_accounts` stamps `invalid_since` on the first confirmed credential
    rejection and retires the account once the grace period is up, so the deadline is
    derivable here instead of being stored.
    """
    item = AccountOut.model_validate(account)
    if account.status == "auth_error" and account.invalid_since is not None:
        retire_at = account.invalid_since + timedelta(days=settings.janitor_account_grace_days)
        item.retire_at = retire_at
        item.retire_action = settings.janitor_account_action
        item.retire_warning = retire_at - datetime.now(UTC) <= timedelta(days=RETIRE_WARN_DAYS)
    return item


async def message_value_kinds(
    session: AsyncSession, message_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    """`message_id -> mined value kinds`, batched for one page of messages."""
    ids = list(dict.fromkeys(message_ids))
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(MessageValue.message_id, MessageValue.kind)
            .where(MessageValue.message_id.in_(ids))
            .distinct()
        )
    ).all()
    grouped: dict[uuid.UUID, set[str]] = {}
    for message_id, kind in rows:
        grouped.setdefault(message_id, set()).add(kind)
    return {
        message_id: [kind for kind in VALUE_KINDS if kind in kinds]
        for message_id, kinds in grouped.items()
    }


async def message_flags(
    session: AsyncSession, message_ids: Iterable[uuid.UUID], folder_id: uuid.UUID | None = None
) -> dict[uuid.UUID, tuple[bool, bool, list[uuid.UUID]]]:
    """`message_id -> (seen, flagged, folder_ids)`.

    With `folder_id`, flags come from that folder placement; without it, a flag is
    considered `True` if any placement has it set (the same message in multiple folders).
    """
    ids = list(dict.fromkeys(message_ids))
    if not ids:
        return {}
    query: Select = select(
        MailboxMessage.message_id,
        MailboxMessage.folder_id,
        MailboxMessage.flag_seen,
        MailboxMessage.flag_flagged,
    ).where(MailboxMessage.message_id.in_(ids))
    if folder_id is not None:
        query = query.where(MailboxMessage.folder_id == folder_id)
    rows = (await session.execute(query)).all()

    result: dict[uuid.UUID, tuple[bool, bool, list[uuid.UUID]]] = {}
    for message_id, placement_folder_id, seen, flagged in rows:
        current = result.get(message_id)
        if current is None:
            result[message_id] = (seen, flagged, [placement_folder_id])
        else:
            result[message_id] = (
                current[0] or seen,
                current[1] or flagged,
                [*current[2], placement_folder_id],
            )
    return result


def addresses(value: object) -> list[AddressOut]:
    if not isinstance(value, list):
        return []
    out: list[AddressOut] = []
    for entry in value:
        if isinstance(entry, dict):
            out.append(AddressOut(name=entry.get("name") or None, addr=entry.get("addr") or ""))
    return out


def message_out(
    row: Message,
    flags: tuple[bool, bool, list[uuid.UUID]] | None,
    value_kinds: list[str] | None = None,
) -> MessageOut:
    seen, flagged, folder_ids = flags if flags else (False, False, [])
    item = MessageOut.model_validate(row)
    item.to = addresses(row.to_addrs)
    item.seen, item.flagged, item.folder_ids = seen, flagged, folder_ids
    item.value_kinds = value_kinds or []
    return item


async def body_out(session: AsyncSession, message: Message) -> BodyOut:
    """Message body + attachment list; used by the REST endpoint and the MCP tool."""
    body = (
        await session.execute(select(MessageBody).where(MessageBody.message_id == message.id))
    ).scalar_one_or_none()
    attachments = (
        await session.execute(
            select(Attachment)
            .where(Attachment.message_id == message.id)
            .order_by(Attachment.filename)
        )
    ).scalars()
    return BodyOut(
        body_state=message.body_state,
        text=body.body_text if body else None,
        html=body.body_html if body else None,
        attachments=[AttachmentOut.model_validate(item) for item in attachments],
    )


async def thread_out(session: AsyncSession, thread: Thread) -> ThreadOut:
    """Every message in a thread, ordered by time; used by REST and the MCP tool."""
    rows = list(
        (
            await session.execute(
                select(Message)
                .where(Message.thread_id == thread.id, Message.account_id == thread.account_id)
                .order_by(func.coalesce(Message.internal_date, Message.created_at))
            )
        )
        .scalars()
        .unique()
        .all()
    )
    ids = [row.id for row in rows]
    flags = await message_flags(session, ids)
    kinds = await message_value_kinds(session, ids)
    return ThreadOut(
        thread_id=thread.id,
        subject=thread.subject_norm,
        message_count=thread.message_count,
        items=[message_out(row, flags.get(row.id), kinds.get(row.id)) for row in rows],
    )
