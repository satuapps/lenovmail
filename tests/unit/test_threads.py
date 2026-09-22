# Lenovmail — authored by satuapps (satuapps.com)
"""Tests thread formation from References/In-Reply-To headers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lenovmail.models import Account, Message, MessageRef, Thread
from lenovmail.sync.normalize import normalize_subject
from lenovmail.sync.threads import assign_thread

BASE_TIME = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)


async def new_message(
    session: AsyncSession,
    account_id: uuid.UUID,
    *,
    message_id: str | None,
    subject: str | None = "Topic",
    offset_minutes: int = 0,
) -> Message:
    """Insert a minimal `messages` row (body not yet fetched)."""
    row = Message(
        account_id=account_id,
        dedup_hash=uuid.uuid4().bytes,
        rfc822_message_id=message_id,
        subject=subject,
        subject_norm=normalize_subject(subject),
        internal_date=BASE_TIME + timedelta(minutes=offset_minutes),
        body_state="none",
    )
    session.add(row)
    await session.flush()
    return row


async def thread_of(session: AsyncSession, message_id: uuid.UUID) -> uuid.UUID | None:
    return (
        await session.execute(select(Message.thread_id).where(Message.id == message_id))
    ).scalar_one()


async def refs_of(session: AsyncSession, message_id: uuid.UUID) -> list[tuple[int, str]]:
    rows = (
        await session.execute(
            select(MessageRef.position, MessageRef.ref)
            .where(MessageRef.message_id == message_id)
            .order_by(MessageRef.position)
        )
    ).all()
    return [(position, ref) for position, ref in rows]


async def add(
    session: AsyncSession,
    account: Account,
    *,
    message_id: str | None,
    refs: list[str],
    subject: str | None = "Topic",
    offset_minutes: int = 0,
) -> Message:
    row = await new_message(
        session, account.id, message_id=message_id, subject=subject, offset_minutes=offset_minutes
    )
    await assign_thread(
        session,
        account_id=account.id,
        message_row_id=row.id,
        msg_id_header=message_id,
        refs=refs,
        subject_norm=normalize_subject(subject),
        internal_date=row.internal_date,
    )
    await session.commit()
    return row


class TestThreadFormation:
    async def test_reply_chain_in_random_order_forms_one_thread(self, session, account):
        """A, Re: A, Re: Re: A arrive out of order -> still one thread."""
        reply2 = await add(
            session,
            account,
            message_id="<c@x>",
            refs=["<a@x>", "<b@x>"],
            subject="Re: Re: Topic",
            offset_minutes=2,
        )
        root = await add(
            session, account, message_id="<a@x>", refs=[], subject="Topic", offset_minutes=0
        )
        reply1 = await add(
            session,
            account,
            message_id="<b@x>",
            refs=["<a@x>"],
            subject="Re: Topic",
            offset_minutes=1,
        )

        threads = {await thread_of(session, m.id) for m in (root, reply1, reply2)}
        assert len(threads) == 1
        assert None not in threads

        thread_id = threads.pop()
        thread = (await session.execute(select(Thread).where(Thread.id == thread_id))).scalar_one()
        assert thread.message_count == 3
        assert thread.last_message_at == BASE_TIME + timedelta(minutes=2)

    async def test_two_threads_joined_by_combined_references(self, session, account):
        """A message referencing two different threads merges them."""
        first = await add(session, account, message_id="<t1@x>", refs=[], subject="Topic one")
        second = await add(
            session, account, message_id="<t2@x>", refs=[], subject="Topic two", offset_minutes=5
        )
        assert await thread_of(session, first.id) != await thread_of(session, second.id)

        bridge = await add(
            session,
            account,
            message_id="<bridge@x>",
            refs=["<t1@x>", "<t2@x>"],
            subject="Merged",
            offset_minutes=10,
        )

        merged = await thread_of(session, bridge.id)
        assert await thread_of(session, first.id) == merged
        assert await thread_of(session, second.id) == merged

        remaining = (await session.execute(select(Thread.id))).scalars().all()
        assert merged in remaining
        assert len([t for t in remaining if t == merged]) == 1

        thread = (await session.execute(select(Thread).where(Thread.id == merged))).scalar_one()
        assert thread.message_count == 3

    async def test_same_subject_does_not_merge_without_reference(self, session, account):
        """Identical subject without header references stays two separate threads."""
        first = await add(session, account, message_id="<s1@x>", refs=[], subject="Report Q3")
        second = await add(
            session, account, message_id="<s2@x>", refs=[], subject="Report Q3", offset_minutes=3
        )

        assert await thread_of(session, first.id) != await thread_of(session, second.id)

    async def test_reply_creates_new_thread_when_reference_is_unknown(self, session, account):
        message = await add(
            session, account, message_id="<y@x>", refs=["<missing@x>"], subject="Re: Unrelated"
        )

        assert await thread_of(session, message.id) is not None

    async def test_subject_norm_names_only_new_thread(self, session, account):
        root = await add(session, account, message_id="<n1@x>", refs=[], subject="Re: New Topic")
        thread_id = await thread_of(session, root.id)
        thread = (await session.execute(select(Thread).where(Thread.id == thread_id))).scalar_one()

        assert thread.subject_norm == "new topic"


class TestReferenceRows:
    async def test_own_message_id_is_position_zero(self, session, account):
        message = await add(
            session, account, message_id="<own@x>", refs=["<parent@x>", "<grand@x>"]
        )

        assert await refs_of(session, message.id) == [
            (0, "<own@x>"),
            (1, "<grand@x>"),
            (2, "<parent@x>"),
        ]

    async def test_reassign_is_idempotent(self, session, account):
        message = await add(session, account, message_id="<idem@x>", refs=["<p@x>"])
        before = await refs_of(session, message.id)

        for _ in range(2):
            await assign_thread(
                session,
                account_id=account.id,
                message_row_id=message.id,
                msg_id_header="<idem@x>",
                refs=["<p@x>"],
                subject_norm="topic",
                internal_date=message.internal_date,
            )
            await session.commit()

        assert await refs_of(session, message.id) == before
        thread = (
            await session.execute(
                select(Thread).where(Thread.id == await thread_of(session, message.id))
            )
        ).scalar_one()
        assert thread.message_count == 1

    async def test_message_without_message_id_still_gets_thread(self, session, account):
        message = await add(session, account, message_id=None, refs=[])

        assert await thread_of(session, message.id) is not None
        assert await refs_of(session, message.id) == []
