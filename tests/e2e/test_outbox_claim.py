# Lenovmail — authored by satuapps (satuapps.com)
"""Claiming outbox rows must be atomic when multiple senders run concurrently.

Regression: before `send_pending` claimed a row (`status='sending'` + `locked_at` in a
single `FOR UPDATE SKIP LOCKED` transaction), two parallel send jobs would pick the same
`queued` row and send the same message twice. This test replaces SMTP sending with a
recorder so it actually exercises row selection, not the network.
"""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import select

from lenovmail.crypto import account_aad, encrypt
from lenovmail.db import SessionLocal
from lenovmail.models import Account, ImapSettings, Outbox
from lenovmail.sync import outbox


async def test_parallel_senders_send_each_row_once(session, account, monkeypatch):
    session.add(
        ImapSettings(
            account_id=account.id,
            host="127.0.0.1",
            port=3143,
            security="none",
            username="demo",
            password_enc=encrypt(account_aad(account.id, "password_enc"), b"demo"),
            smtp_host="127.0.0.1",
            smtp_port=3025,
            smtp_security="none",
            smtp_username="demo",
        )
    )
    await session.commit()

    sent_messages: list[str] = []

    async def fake_send(config, raw: bytes, *, envelope_from: str, recipients: list[str]) -> None:
        sent_messages.append(raw.decode("utf-8", "replace"))

    monkeypatch.setattr(outbox, "smtp_send_message", fake_send)

    subjects = [f"claim-{uuid.uuid4().hex[:8]}-{index}" for index in range(3)]
    for subject in subjects:
        await outbox.queue(
            session,
            account,
            payload={
                "from": account.email_address,
                "to": ["demo@localhost"],
                "cc": [],
                "bcc": [],
                "subject": subject,
                "text": "claim test",
                "html": None,
                "references": [],
                "attachments": [],
            },
            requires_approval=False,
        )
    account_id = account.id

    async def deliver() -> int:
        async with SessionLocal() as own_session:
            fresh = await own_session.get(Account, account_id)
            assert fresh is not None
            return await outbox.send_pending(own_session, fresh, limit=10)

    results = await asyncio.gather(*(deliver() for _ in range(4)))
    assert sum(results) == len(subjects), f"claim count mismatch: {results}"
    for subject in subjects:
        sent_count = sum(1 for raw in sent_messages if subject in raw)
        assert sent_count == 1, f"{subject} was sent {sent_count} times"

    async with SessionLocal() as check:
        rows = (
            await check.execute(select(Outbox.status).where(Outbox.account_id == account_id))
        ).scalars()
        assert list(rows) == ["sent"] * len(subjects)
