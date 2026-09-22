# Lenovmail — authored by satuapps (satuapps.com)
"""`GET /api/messages` searches every account the caller may read — and nothing else.

Cross-account search is the one read path where the account id is not in the URL, so the
allowlist on an agent token is the only thing standing between a scoped token and another
mailbox. These tests pin that boundary.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

from lenovmail.api.app import create_app
from lenovmail.api.security import new_agent_token
from lenovmail.models import Account, AgentToken, Message, MessageValue


async def add_message(session: AsyncSession, account_id: uuid.UUID, subject: str) -> Message:
    row = Message(
        account_id=account_id,
        subject=subject,
        dedup_hash=hashlib.sha256(subject.encode()).digest(),
        internal_date=datetime.now(UTC),
        body_state="full",
    )
    session.add(row)
    await session.commit()
    return row


async def issue_token(
    session: AsyncSession,
    owner_id: uuid.UUID,
    *,
    account_ids: list[uuid.UUID] | None = None,
) -> str:
    plain, digest = new_agent_token()
    session.add(
        AgentToken(
            owner_id=owner_id,
            name=f"search-{uuid.uuid4().hex[:8]}",
            token_hash=digest,
            scopes=["mail.read"],
            account_ids=account_ids,
        )
    )
    await session.commit()
    return plain


async def get(path: str, token: str) -> httpx.Response:
    transport = ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        return await client.get(path, headers={"Authorization": f"Bearer {token}"})


@pytest.fixture
async def second_account(session: AsyncSession, account: Account) -> Account:
    row = Account(
        owner_id=account.owner_id,
        email_address=f"second-{uuid.uuid4().hex[:8]}@lenov.test",
        provider="imap",
    )
    session.add(row)
    await session.commit()
    return row


async def test_unrestricted_token_sees_every_account(session, account, second_account):
    await add_message(session, account.id, "first account message")
    await add_message(session, second_account.id, "second account message")
    token = await issue_token(session, account.owner_id)

    response = await get("/api/messages?limit=50", token)

    assert response.status_code == 200
    accounts = {item["account_id"] for item in response.json()["items"]}
    assert accounts == {str(account.id), str(second_account.id)}


async def test_allowlisted_token_sees_only_its_account(session, account, second_account):
    await add_message(session, account.id, "first account message")
    await add_message(session, second_account.id, "second account message")
    token = await issue_token(session, account.owner_id, account_ids=[account.id])

    response = await get("/api/messages?limit=50", token)

    assert response.status_code == 200
    accounts = {item["account_id"] for item in response.json()["items"]}
    assert accounts == {str(account.id)}


async def test_allowlisted_token_cannot_name_another_account(session, account, second_account):
    token = await issue_token(session, account.owner_id, account_ids=[account.id])

    response = await get(f"/api/messages?account_id={second_account.id}", token)

    # 404, not 403: a token must not be able to probe which account ids exist.
    assert response.status_code == 404
    assert response.json()["detail"] == "account not found"


async def test_value_kind_filter_narrows_to_mined_messages(session, account):
    plain = await add_message(session, account.id, "nothing mined here")
    mined = await add_message(session, account.id, "Your verification code is 482913")
    session.add(MessageValue(message_id=mined.id, kind="otp", value="482913", confidence=90))
    await session.commit()
    token = await issue_token(session, account.owner_id)

    response = await get("/api/messages?value_kind=otp", token)

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["id"] for item in items] == [str(mined.id)]
    assert items[0]["value_kinds"] == ["otp"]
    assert str(plain.id) not in {item["id"] for item in items}


async def test_unknown_value_kind_is_rejected(session, account):
    token = await issue_token(session, account.owner_id)

    response = await get("/api/messages?value_kind=nope", token)

    assert response.status_code == 400
    assert "nope" in response.json()["detail"]
