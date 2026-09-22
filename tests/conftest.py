# Lenovmail — authored by satuapps
"""Shared fixtures: a real database session and a temporary account.

Tests that need a database are **skipped**, not failed, when Postgres can't be reached
(e.g. `docker compose up -d db` hasn't been run yet).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from lenovmail.db import SessionLocal, engine
from lenovmail.models import Account, Folder, User

# Test IMAP server port (GreenMail) — used by e2e tests.
TEST_IMAP_PORT = 3143
TEST_SMTP_PORT = 3025


async def database_reachable() -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("select 1"))
    except Exception:
        return False
    return True


@pytest.fixture(autouse=True)
async def _dispose_engine() -> AsyncIterator[None]:
    """Drop pool connections after each test.

    pytest-asyncio creates a new event loop per test; asyncpg connections still in the
    pool are bound to the old loop and will fail if reused.
    """
    yield
    await engine.dispose()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    if not await database_reachable():
        pytest.skip("Postgres unavailable (run: docker compose up -d db)")
    async with SessionLocal() as sess:
        yield sess


@pytest.fixture
async def account(session: AsyncSession) -> AsyncIterator[Account]:
    """Account owned by a new user; all of its rows are deleted again after the test."""
    tag = uuid.uuid4().hex[:12]
    user = User(email=f"user-{tag}@lenov.test", password_hash="x", role="user")
    session.add(user)
    await session.flush()
    # Fetched before the yield: any rollback inside the test makes the instance expired,
    # and reading its attributes afterward triggers a synchronous lazy-load (MissingGreenlet).
    user_id = user.id

    acct = Account(owner_id=user_id, email_address=f"acct-{tag}@lenov.test", provider="imap")
    session.add(acct)
    await session.commit()

    yield acct

    # CASCADE from users deletes the account along with its folders/messages/threads.
    await session.execute(delete(User).where(User.id == user_id))
    await session.commit()


@pytest.fixture
async def folder(session: AsyncSession, account: Account) -> Folder:
    """Test account's INBOX folder; also deleted via CASCADE from `account`."""
    row = Folder(
        account_id=account.id,
        remote_id="INBOX",
        name="INBOX",
        path="INBOX",
        role="inbox",
        sync_pass=0,
    )
    session.add(row)
    await session.commit()
    return row
