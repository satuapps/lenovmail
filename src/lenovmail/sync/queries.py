# Lenovmail — authored by satuapps (satuapps.com)
"""Message read query shared by the REST API and MCP server.

Filter rules (folder, status, full-text search, keyset cursor) must be identical for
the GUI and agents, so there is only one implementation here. The transport layer is
responsible for turning domain errors into HTTP status codes or tool errors.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Folder, MailboxMessage, Message, MessageSearch
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
    account_id: uuid.UUID,
    *,
    folder_id: uuid.UUID | None = None,
    q: str | None = None,
    unread: bool | None = None,
    flagged: bool | None = None,
    has_attachments: bool | None = None,
    thread_id: uuid.UUID | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> tuple[list[Message], str | None]:
    """Fetch one page of the newest messages; return the rows and the next cursor."""
    query: Select = select(Message).where(Message.account_id == account_id)

    if folder_id is not None:
        folder = await session.get(Folder, folder_id)
        if folder is None or folder.account_id != account_id:
            raise FolderNotFoundError("folder not found")
        query = query.join(MailboxMessage, MailboxMessage.message_id == Message.id).where(
            MailboxMessage.folder_id == folder_id
        )
        if unread is not None:
            query = query.where(MailboxMessage.flag_seen.is_(not unread))
        if flagged is not None:
            query = query.where(MailboxMessage.flag_flagged.is_(flagged))
    if has_attachments is not None:
        query = query.where(Message.has_attachments.is_(has_attachments))
    if thread_id is not None:
        query = query.where(Message.thread_id == thread_id)
    if q:
        query = query.join(MessageSearch, MessageSearch.message_id == Message.id).where(
            MessageSearch.tsv.op("@@")(tsquery_expr(q))
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
