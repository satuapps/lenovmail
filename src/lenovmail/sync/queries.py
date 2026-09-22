# Lenovmail — authored by satuapps (satuapps.com)
"""Message read query shared by the REST API and MCP server.

Filter rules (folder, status, full-text search, keyset cursor) must be identical for
the GUI and agents, so there is only one implementation here. The transport layer is
responsible for turning domain errors into HTTP status codes or tool errors.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Folder, MailboxMessage, Message, MessageSearch, MessageValue
from ..search import tsquery_expr

CURSOR_VERSION = "1"


class FolderNotFoundError(Exception):
    """Folder does not exist or does not belong to the requested account."""


def encode_cursor(row: Message) -> str:
    """Keyset cursor: message timestamp + id, so the next page never uses OFFSET."""
    stamp = (row.internal_date or row.sent_date or row.created_at).isoformat()
    raw = f"{CURSOR_VERSION}|{stamp}|{row.id}"
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    padded = cursor + "=" * (-len(cursor) % 4)
    raw = base64.urlsafe_b64decode(padded.encode()).decode()
    version, stamp, raw_id = raw.split("|", 2)
    if version != CURSOR_VERSION:
        raise ValueError("unrecognized cursor version")
    return datetime.fromisoformat(stamp), uuid.UUID(raw_id)


async def list_messages(
    session: AsyncSession,
    account_ids: Sequence[uuid.UUID],
    *,
    folder_id: uuid.UUID | None = None,
    q: str | None = None,
    unread: bool | None = None,
    flagged: bool | None = None,
    has_attachments: bool | None = None,
    thread_id: uuid.UUID | None = None,
    value_kinds: Sequence[str] | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[Message], str | None]:
    """Fetch one page of the newest messages; return the rows and the next cursor.

    `account_ids` may hold one account (a mailbox view) or many (search across accounts);
    the keyset is ordered on time and id alone, so paging works the same either way.
    """
    if not account_ids:
        return [], None

    query: Select = select(Message).where(Message.account_id.in_(account_ids))

    if folder_id is not None:
        if len(account_ids) != 1:
            # Unreachable over HTTP (the cross-account endpoint has no folder parameter);
            # a future caller should fail here instead of silently ignoring the folder.
            raise ValueError("folder_id requires exactly one account")
        folder = await session.get(Folder, folder_id)
        if folder is None or folder.account_id != account_ids[0]:
            raise FolderNotFoundError("folder not found")
        query = query.join(MailboxMessage, MailboxMessage.message_id == Message.id).where(
            MailboxMessage.folder_id == folder_id
        )
        if unread is not None:
            query = query.where(MailboxMessage.flag_seen.is_(not unread))
        if flagged is not None:
            query = query.where(MailboxMessage.flag_flagged.is_(flagged))
    else:
        # Without a folder the flags live on any placement of the message, so they are
        # asked for with EXISTS instead of a join that would multiply the rows.
        placement = (
            select(1).select_from(MailboxMessage).where(MailboxMessage.message_id == Message.id)
        )
        if unread is not None:
            query = query.where(placement.where(MailboxMessage.flag_seen.is_(not unread)).exists())
        if flagged is not None:
            query = query.where(placement.where(MailboxMessage.flag_flagged.is_(flagged)).exists())
    if has_attachments is not None:
        query = query.where(Message.has_attachments.is_(has_attachments))
    if thread_id is not None:
        query = query.where(Message.thread_id == thread_id)
    if q:
        query = query.join(MessageSearch, MessageSearch.message_id == Message.id).where(
            MessageSearch.tsv.op("@@")(tsquery_expr(q))
        )
    if value_kinds:
        query = query.where(
            select(1)
            .select_from(MessageValue)
            .where(MessageValue.message_id == Message.id, MessageValue.kind.in_(value_kinds))
            .exists()
        )

    if cursor:
        stamp, last_id = decode_cursor(cursor)
        query = query.where(
            or_(
                func.coalesce(Message.internal_date, Message.created_at) < stamp,
                and_(
                    func.coalesce(Message.internal_date, Message.created_at) == stamp,
                    Message.id < last_id,
                ),
            )
        )

    query = query.order_by(
        func.coalesce(Message.internal_date, Message.created_at).desc(), Message.id.desc()
    ).limit(limit + 1)

    rows = list((await session.execute(query)).scalars().unique().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    return rows, encode_cursor(rows[-1]) if has_more and rows else None
