# Lenovmail — authored by satuapps (satuapps.com)
"""Message thread formation from `Message-ID`/`References`/`In-Reply-To` headers.

The rule is conservative: threads only merge when a header reference genuinely matches
within the same account. `subject_norm` is only used to name a new thread,
**never** to merge — two different conversations can share an identical subject.

This module never commits; the caller manages the transaction.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..logging import get_logger
from ..models import Message, MessageRef, Thread

log = get_logger(__name__)


async def _threads_referenced_by(
    session: AsyncSession, account_id: uuid.UUID, refs: set[str]
) -> list[uuid.UUID]:
    """Threads containing any of `refs`, ordered from the earliest conversation."""
    if not refs:
        return []
    statement = (
        select(Message.thread_id)
        .join(MessageRef, MessageRef.message_id == Message.id)
        .where(
            Message.account_id == account_id,
            Message.thread_id.is_not(None),
            MessageRef.ref.in_(sorted(refs)),
        )
        .group_by(Message.thread_id)
        .order_by(func.min(Message.internal_date).asc().nulls_last())
    )
    rows = (await session.execute(statement)).scalars().all()
    return [row for row in rows if row is not None]


async def _merge_threads(
    session: AsyncSession, canonical: uuid.UUID, others: list[uuid.UUID]
) -> None:
    if not others:
        return
    await session.execute(
        update(Message).where(Message.thread_id.in_(others)).values(thread_id=canonical)
    )
    await session.execute(delete(Thread).where(Thread.id.in_(others)))
    log.info("threads_merged", canonical=str(canonical), merged=len(others))


async def _refresh_thread_counters(
    session: AsyncSession, thread_id: uuid.UUID, fallback_at: datetime | None
) -> None:
    statement = select(
        func.count(Message.id), func.max(func.coalesce(Message.internal_date, Message.created_at))
    ).where(Message.thread_id == thread_id)
    count, last_at = (await session.execute(statement)).one()
    await session.execute(
        update(Thread)
        .where(Thread.id == thread_id)
        .values(message_count=count or 0, last_message_at=last_at or fallback_at)
    )


async def assign_thread(
    session: AsyncSession,
    *,
    account_id: uuid.UUID,
    message_row_id: uuid.UUID,
    msg_id_header: str | None,
    refs: list[str],
    subject_norm: str | None,
    internal_date: datetime | None,
) -> uuid.UUID:
    """Determine the thread for `message_row_id` and record its header references.

    Returns the thread id. A new thread is created when no reference matches; when several
    threads match, they're merged into one (the earliest wins).
    """
    own = (msg_id_header or "").strip() or None
    candidates = {ref.strip() for ref in refs if ref and ref.strip()}
    if own:
        candidates.add(own)

    matched = await _threads_referenced_by(session, account_id, candidates)

    if matched:
        thread_id = matched[0]
        await _merge_threads(session, thread_id, matched[1:])
        if subject_norm:
            await session.execute(
                update(Thread)
                .where(Thread.id == thread_id, Thread.subject_norm.is_(None))
                .values(subject_norm=subject_norm)
            )
    else:
        thread = Thread(
            account_id=account_id,
            subject_norm=subject_norm,
            last_message_at=internal_date,
            message_count=0,
        )
        session.add(thread)
        await session.flush()
        thread_id = thread.id

    await session.execute(
        update(Message).where(Message.id == message_row_id).values(thread_id=thread_id)
    )

    # Idempotent: this message's references are rewritten, not appended.
    await session.execute(delete(MessageRef).where(MessageRef.message_id == message_row_id))
    rows: list[MessageRef] = []
    position = 0
    if own:
        rows.append(MessageRef(message_id=message_row_id, position=position, ref=own))
        position += 1
    for ref in dict.fromkeys(sorted(candidates)):
        if ref == own:
            continue
        rows.append(MessageRef(message_id=message_row_id, position=position, ref=ref))
        position += 1
    if rows:
        session.add_all(rows)

    await _refresh_thread_counters(session, thread_id, internal_date)
    return thread_id
