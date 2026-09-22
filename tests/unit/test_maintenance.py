# Lenovmail — authored by satuapps (satuapps.com)
"""Janitor decisions: quarantine, recovery, retirement, and token purging.

The risky part of this module is not the SQL, it is the classification. A transient outage
must never retire a mailbox, and a grace period measured from the wrong timestamp deletes
accounts early. Both are pinned here with a stubbed credential checker, so the tests never
touch a real IMAP or Graph server.

The checker answers `unknown` for every account except the one under test: the sweep runs
against the whole table, and a leftover row in a developer's database must not change what
these assertions mean.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from lenovmail.maintenance import (
    CredentialCheck,
    CredentialState,
    purge_agent_audit,
    purge_agent_tokens,
    sweep_accounts,
)
from lenovmail.models import Account, AgentAudit, AgentToken

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
GRACE = timedelta(days=14)


def answers(target: uuid.UUID, state: CredentialState, detail: str | None = "login rejected"):
    """Credential checker that only judges `target`; everything else looks unreachable."""
    seen: list[uuid.UUID] = []

    async def check(_session, account: Account) -> CredentialCheck:
        seen.append(account.id)
        if account.id != target:
            return CredentialCheck("unknown", "not part of this test")
        return CredentialCheck(state, detail)

    check.seen = seen  # type: ignore[attr-defined]
    return check


async def quarantine(session, account: Account, *, invalid_since: datetime | None) -> None:
    account.status = "auth_error"
    account.status_detail = "login rejected"
    account.invalid_since = invalid_since
    await session.commit()


class TestAccountSweep:
    async def test_working_credentials_return_the_account_to_active(self, session, account):
        await quarantine(session, account, invalid_since=NOW - timedelta(days=30))

        report = await sweep_accounts(
            session, now=NOW, grace=GRACE, check=answers(account.id, "valid", None)
        )

        await session.refresh(account)
        assert report.recovered == 1 and report.deleted == 0 and report.disabled == 0
        assert account.status == "active"
        assert account.status_detail is None
        assert account.invalid_since is None

    async def test_first_rejection_only_starts_the_clock(self, session, account):
        await quarantine(session, account, invalid_since=None)

        report = await sweep_accounts(
            session, now=NOW, grace=GRACE, check=answers(account.id, "invalid")
        )

        await session.refresh(account)
        assert report.quarantined == 1 and report.disabled == 0 and report.deleted == 0
        assert account.status == "auth_error"
        assert account.invalid_since == NOW

    async def test_rejection_inside_the_grace_period_keeps_the_account(self, session, account):
        stamped = NOW - timedelta(days=13, hours=23)
        await quarantine(session, account, invalid_since=stamped)

        report = await sweep_accounts(
            session, now=NOW, grace=GRACE, action="delete", check=answers(account.id, "invalid")
        )

        await session.refresh(account)
        assert report.disabled == 0 and report.deleted == 0 and report.quarantined == 0
        assert account.status == "auth_error"
        assert account.invalid_since == stamped

    async def test_rejection_past_the_grace_period_disables_by_default(self, session, account):
        await quarantine(session, account, invalid_since=NOW - timedelta(days=15))

        report = await sweep_accounts(
            session,
            now=NOW,
            grace=GRACE,
            action="disable",
            check=answers(account.id, "invalid", "app password withdrawn"),
        )

        await session.refresh(account)
        assert report.disabled == 1 and report.deleted == 0
        assert account.status == "disabled"
        assert account.status_detail == "app password withdrawn"

    async def test_delete_action_removes_the_account_row(self, session, account):
        account_id = account.id
        await quarantine(session, account, invalid_since=NOW - timedelta(days=15))

        report = await sweep_accounts(
            session, now=NOW, grace=GRACE, action="delete", check=answers(account_id, "invalid")
        )

        assert report.deleted == 1
        assert await session.get(Account, account_id) is None

    async def test_unreachable_provider_never_retires_an_account(self, session, account):
        stamped = NOW - timedelta(days=400)
        await quarantine(session, account, invalid_since=stamped)

        report = await sweep_accounts(
            session,
            now=NOW,
            grace=GRACE,
            action="delete",
            check=answers(account.id, "unknown", "connection timed out"),
        )

        await session.refresh(account)
        assert report.deleted == 0 and report.disabled == 0 and report.quarantined == 0
        assert account.status == "auth_error"
        assert account.invalid_since == stamped

    async def test_active_accounts_are_never_probed(self, session, account):
        check = answers(account.id, "invalid")

        await sweep_accounts(session, now=NOW, grace=GRACE, action="delete", check=check)

        assert account.status == "active"
        assert account.id not in check.seen
        assert await session.get(Account, account.id) is not None


class TestTokenPurge:
    async def _token(self, session, account, **kwargs) -> AgentToken:
        row = AgentToken(
            owner_id=account.owner_id,
            name="janitor-test",
            token_hash=uuid.uuid4().bytes,
            scopes=["mail.read"],
            **kwargs,
        )
        session.add(row)
        await session.commit()
        return row

    async def test_expired_and_revoked_tokens_are_deleted_after_retention(self, session, account):
        long_expired = await self._token(session, account, expires_at=NOW - timedelta(days=30))
        long_revoked = await self._token(session, account, revoked_at=NOW - timedelta(days=30))
        live = await self._token(session, account, expires_at=NOW + timedelta(days=30))
        just_revoked = await self._token(session, account, revoked_at=NOW - timedelta(hours=1))
        ids = {long_expired.id, long_revoked.id, live.id, just_revoked.id}

        await purge_agent_tokens(session, now=NOW, retention=timedelta(days=7))

        remaining = set(
            (await session.execute(select(AgentToken.id).where(AgentToken.id.in_(ids))))
            .scalars()
            .all()
        )
        assert remaining == {live.id, just_revoked.id}

    async def test_audit_trail_outlives_the_token_it_references(self, session, account):
        token = await self._token(session, account, revoked_at=NOW - timedelta(days=30))
        marker = f"mcp.test_{uuid.uuid4().hex[:8]}"
        session.add(AgentAudit(token_id=token.id, tool=marker, outcome="ok"))
        await session.commit()

        await purge_agent_tokens(session, now=NOW, retention=timedelta(days=7))

        row = (
            await session.execute(select(AgentAudit).where(AgentAudit.tool == marker))
        ).scalar_one()
        assert row.token_id is None
        await session.execute(delete(AgentAudit).where(AgentAudit.id == row.id))
        await session.commit()

    async def test_audit_retention_of_zero_keeps_every_row(self, session, account):
        marker = f"mcp.test_{uuid.uuid4().hex[:8]}"
        session.add(AgentAudit(tool=marker, outcome="ok"))
        await session.commit()

        assert await purge_agent_audit(session, now=NOW, retention=timedelta(0)) == 0

        row = (
            await session.execute(select(AgentAudit).where(AgentAudit.tool == marker))
        ).scalar_one()
        await session.execute(delete(AgentAudit).where(AgentAudit.id == row.id))
        await session.commit()
