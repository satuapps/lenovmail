# Lenovmail — authored by satuapps (satuapps.com)
"""The transient-failure path in `sync_account` must not crash the job, and must not
masquerade as success.

Two regressions are guarded here:

* `sync_folder` calls `rollback()` on failure, and the rollback expires ORM instances in the
  same session. Before the fix, the error handler read `folder.id` / `account.id` from the
  expired instance, triggering a synchronous lazy-load (`MissingGreenlet`) and causing the
  `sync_account` job to fail entirely even though only one folder had a problem.
* If **not a single** folder succeeds due to a transient outage, the account must not be
  marked `active` — the outage must be visible in the GUI as `error`.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from sqlalchemy import select

from lenovmail.crypto import account_aad, encrypt
from lenovmail.db import SessionLocal
from lenovmail.models import Account, Folder, ImapSettings
from lenovmail.providers.imap_pool import MailTransientError
from lenovmail.workers import tasks


class FakePool:
    """Fake pool: `connection()` only needs to work as an async context manager."""

    def connection(self) -> FakePool:
        return self

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


async def _prepare(session, account: Account) -> uuid.UUID:
    session.add(
        ImapSettings(
            account_id=account.id,
            host="127.0.0.1",
            port=3143,
            security="none",
            username="demo",
            password_enc=encrypt(account_aad(account.id, "password_enc"), b"demo"),
        )
    )
    await session.commit()
    # Stored as a plain value: rollback inside `sync_folder` expires the `account`/`folder`
    # instances, and reading their attributes afterward triggers a synchronous lazy-load.
    return account.id


async def _install_fake_pool(monkeypatch) -> None:
    async def fake_pool(*args, **kwargs) -> FakePool:
        return FakePool()

    async def fake_sync_folders(*args, **kwargs) -> list:
        # Folder discovery has its own test; the focus here is per-folder error handling.
        return []

    monkeypatch.setattr(tasks, "get_account_pool", fake_pool)
    monkeypatch.setattr(tasks.imap_sync, "sync_folders", fake_sync_folders)


async def _fake_bodies(*args, **kwargs) -> int:
    return 0


async def _status_of(account_id: uuid.UUID) -> tuple[str, str | None]:
    async with SessionLocal() as check:
        row = (await check.execute(select(Account).where(Account.id == account_id))).scalar_one()
        return row.status, row.status_detail


async def test_partial_transient_failure_keeps_account_active(
    session, account, folder, monkeypatch
):
    """One folder fails transiently, another succeeds: job completes, status stays `active`."""
    account_uuid = await _prepare(session, account)
    session.add(
        Folder(
            account_id=account_uuid,
            remote_id="Archive",
            name="Archive",
            path="Archive",
            role="archive",
        )
    )
    await session.commit()
    await _install_fake_pool(monkeypatch)

    async def partial_sync(session_, account_, folder_, conn, **kwargs):
        if folder_.remote_id == "INBOX":
            await session_.rollback()
            raise MailTransientError("command: SELECT => socket error: EOF")
        return SimpleNamespace(added=1, updated=0, removed=0)

    monkeypatch.setattr(tasks.imap_sync, "sync_folder", partial_sync)
    monkeypatch.setattr(tasks.imap_sync, "fetch_bodies", _fake_bodies)

    result = await tasks._sync_imap_account(session, account)

    assert result["folders"] == 1
    assert result["added"] == 1
    status, detail = await _status_of(account_uuid)
    assert status == "active", "partial folder success is not an account failure"
    assert detail is None


async def test_all_folders_failing_marks_account_error(session, account, folder, monkeypatch):
    """No folder succeeds: the account is marked `error` with outage detail."""
    account_uuid = await _prepare(session, account)
    await _install_fake_pool(monkeypatch)

    async def failing_sync(session_, *args, **kwargs):
        await session_.rollback()
        raise MailTransientError("IMAP connection failed: Connection refused")

    monkeypatch.setattr(tasks.imap_sync, "sync_folder", failing_sync)

    result = await tasks._sync_imap_account(session, account)

    assert result["folders"] == 0
    assert "refused" in str(result["error"])
    status, detail = await _status_of(account_uuid)
    assert status == "error"
    assert detail is not None and "refused" in detail


async def test_unreachable_pool_marks_account_error(session, account, monkeypatch):
    """Failure to set up the pool (server down) also ends in status `error`."""
    account_uuid = await _prepare(session, account)

    async def failing_pool(*args, **kwargs):
        raise MailTransientError("IMAP server connection refused")

    monkeypatch.setattr(tasks, "get_account_pool", failing_pool)

    result = await tasks._sync_imap_account(session, account)

    assert result["folders"] == 0
    assert "refused" in str(result["error"])
    status, detail = await _status_of(account_uuid)
    assert status == "error"
    assert detail is not None and "refused" in detail
