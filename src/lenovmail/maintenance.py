# Lenovmail — authored by satuapps
"""Janitor for dead credentials and stale agent tokens.

The sync path flags an account `auth_error` the moment a provider rejects the login, but
nothing ever clears that state again: a mailbox whose app password was withdrawn keeps its
folders, its IDLE watcher slot and its outbox rows forever, and every cycle keeps spending a
login attempt on it. This module closes the loop. Quarantined accounts are re-checked against
the provider, the ones that answer again return to `active`, and the ones still rejected
after `janitor_account_grace_days` are retired — disabled by default, deleted when the
deployment asks for it.

A rejected login and an unreachable server are not the same failure. Only `MailAuthError`,
`AccountConfigError`, `GraphAuthError` and HTTP 401/403 count as invalid; timeouts, DNS
failures and 5xx leave the row untouched. Without that split, an hour of provider downtime
would retire every mailbox in the database.

Agent tokens are simpler: expired or revoked rows are deleted once they are older than the
retention window. `agent_audit.token_id` is `ON DELETE SET NULL`, so the audit trail outlives
the token it points at.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .logging import get_logger
from .models import Account, AgentAudit, AgentToken
from .providers.accounts import (
    AccountConfigError,
    get_account_pool,
    graph_client_for,
    save_graph_tokens,
)
from .providers.graph import GraphAuthError, GraphRequestError, GraphTransientError
from .providers.imap_pool import (
    MailAuthError,
    MailPermanentError,
    MailTransientError,
    reset_pool,
)
from .sync.store import execute_count

log = get_logger(__name__)

CredentialState = Literal["valid", "invalid", "unknown"]
RetireAction = Literal["disable", "delete"]

# Graph answers 403 when the mailbox is gone or the admin pulled the app grant; both need a
# human to reconnect the account, so they are treated like a rejected token.
_GRAPH_INVALID_STATUS = (401, 403)


@dataclass(slots=True)
class CredentialCheck:
    state: CredentialState
    detail: str | None = None


@dataclass(slots=True)
class SweepReport:
    """What one janitor pass did. Returned by the arq job and printed by the CLI."""

    checked: int = 0
    recovered: int = 0
    quarantined: int = 0
    disabled: int = 0
    deleted: int = 0
    unreachable: int = 0
    tokens_deleted: int = 0
    audit_rows_deleted: int = 0

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


CredentialChecker = Callable[[AsyncSession, Account], Awaitable[CredentialCheck]]


async def _check_imap(session: AsyncSession, account: Account) -> CredentialCheck:
    try:
        pool = await get_account_pool(session, account)
    except (MailAuthError, AccountConfigError) as exc:
        return CredentialCheck("invalid", str(exc))
    except MailTransientError as exc:
        return CredentialCheck("unknown", str(exc))

    try:
        async with pool.connection() as conn:
            await conn.noop()
    except (MailAuthError, AccountConfigError) as exc:
        return CredentialCheck("invalid", str(exc))
    except (MailTransientError, MailPermanentError, OSError) as exc:
        return CredentialCheck("unknown", str(exc))
    return CredentialCheck("valid")


async def _check_graph(session: AsyncSession, account: Account) -> CredentialCheck:
    try:
        client = await graph_client_for(session, account)
    except AccountConfigError as exc:
        return CredentialCheck("invalid", str(exc))

    try:
        await client.me()
    except GraphAuthError as exc:
        return CredentialCheck("invalid", str(exc))
    except GraphRequestError as exc:
        if exc.status_code in _GRAPH_INVALID_STATUS:
            return CredentialCheck("invalid", str(exc))
        return CredentialCheck("unknown", str(exc))
    except (GraphTransientError, OSError) as exc:
        return CredentialCheck("unknown", str(exc))
    else:
        # A silent refresh may have issued a new refresh token; losing it would turn a
        # healthy account back into an `auth_error` on the next cycle.
        await save_graph_tokens(session, account.id, client)
        return CredentialCheck("valid")
    finally:
        await client.aclose()


async def check_account(session: AsyncSession, account: Account) -> CredentialCheck:
    """Ask the provider whether the stored credentials still work."""
    if account.provider == "graph":
        return await _check_graph(session, account)
    return await _check_imap(session, account)


async def sweep_accounts(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    grace: timedelta | None = None,
    action: RetireAction | None = None,
    limit: int | None = None,
    check: CredentialChecker = check_account,
) -> SweepReport:
    """Re-check quarantined accounts and retire the ones that stay rejected.

    Only rows already marked `auth_error` are touched: the sync path is what puts them there,
    so a healthy mailbox is never probed by the janitor. `invalid_since` is the clock — it is
    stamped on the first confirmed rejection and cleared as soon as the account answers again.
    """
    now = now or datetime.now(UTC)
    grace = timedelta(days=settings.janitor_account_grace_days) if grace is None else grace
    action = action or settings.janitor_account_action
    limit = limit or settings.janitor_check_batch

    report = SweepReport()
    accounts = (
        (
            await session.execute(
                select(Account)
                .where(Account.status == "auth_error")
                .order_by(Account.updated_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    for account in accounts:
        account_id = str(account.id)
        result = await check(session, account)
        report.checked += 1

        if result.state == "unknown":
            report.unreachable += 1
            log.info("janitor_check_inconclusive", account_id=account_id, error=result.detail)
            continue

        if result.state == "valid":
            account.status = "active"
            account.status_detail = None
            account.invalid_since = None
            report.recovered += 1
            log.info("janitor_account_recovered", account_id=account_id)
            continue

        account.status_detail = (result.detail or "credentials rejected")[:2000]
        await reset_pool(account_id)

        if account.invalid_since is None:
            account.invalid_since = now
            report.quarantined += 1
            log.info("janitor_account_quarantined", account_id=account_id, error=result.detail)
            continue

        if now - account.invalid_since < grace:
            continue

        if action == "delete":
            await session.delete(account)
            report.deleted += 1
            log.warning("janitor_account_deleted", account_id=account_id, error=result.detail)
        else:
            account.status = "disabled"
            report.disabled += 1
            log.warning("janitor_account_disabled", account_id=account_id, error=result.detail)

    await session.commit()
    return report


async def purge_agent_tokens(
    session: AsyncSession, *, now: datetime | None = None, retention: timedelta | None = None
) -> int:
    """Delete revoked and expired agent tokens once they are past the retention window.

    The window exists so an operator can still see (and explain) a token that was revoked an
    hour ago; after that the row is dead weight, and keeping hashes of dead credentials around
    only widens the blast radius of a database leak.
    """
    now = now or datetime.now(UTC)
    if retention is None:
        retention = timedelta(days=settings.janitor_token_retention_days)
    cutoff = now - retention

    result = await execute_count(
        session,
        delete(AgentToken).where(
            or_(
                and_(AgentToken.revoked_at.is_not(None), AgentToken.revoked_at < cutoff),
                and_(AgentToken.expires_at.is_not(None), AgentToken.expires_at < cutoff),
            )
        ),
    )
    await session.commit()
    return result


async def purge_agent_audit(
    session: AsyncSession, *, now: datetime | None = None, retention: timedelta | None = None
) -> int:
    """Trim `agent_audit` to the configured retention. A retention of 0 keeps every row."""
    now = now or datetime.now(UTC)
    if retention is None:
        retention = timedelta(days=settings.janitor_audit_retention_days)
    if retention <= timedelta(0):
        return 0

    cutoff = now - retention
    removed = await execute_count(session, delete(AgentAudit).where(AgentAudit.created_at < cutoff))
    await session.commit()
    return removed


async def run_sweep(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    action: RetireAction | None = None,
    check: CredentialChecker = check_account,
) -> SweepReport:
    """One full janitor pass: accounts first, then tokens, then the audit trail."""
    now = now or datetime.now(UTC)
    report = await sweep_accounts(session, now=now, action=action, check=check)
    report.tokens_deleted = await purge_agent_tokens(session, now=now)
    report.audit_rows_deleted = await purge_agent_audit(session, now=now)
    return report
