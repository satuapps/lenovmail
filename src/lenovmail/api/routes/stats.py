# Lenovmail — authored by satuapps (satuapps.com)
"""Aggregate views: the dashboard counters and the ops console.

Both endpoints answer for the accounts the caller may read, so an agent token with an
account allowlist sees numbers for its own slice rather than the owner's whole estate.

The ops view deliberately keeps `error` and `auth_error` apart. An `error` account is one
whose server did not answer — the sync cron keeps retrying it and it may well recover on
its own. An `auth_error` account had its credentials refused, nothing will fix it but a
human, and the janitor is counting down to retiring it. Merging them into one "broken"
list is what makes operators ignore both.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from ...config import settings
from ...models import Account, Folder, MailboxMessage, Message, Outbox, SyncRun
from ..deps import PrincipalDep, SessionDep, readable_account_ids, require_read
from ..schemas import (
    JanitorOut,
    OpsSummaryOut,
    OverviewOut,
    ProviderOps,
    ProviderStats,
)
from ..serializers import account_out, accounts_out

router = APIRouter(prefix="/api", tags=["stats"])

# Rows are emitted for both providers even when one is unused, so the GUI tables keep a
# stable shape instead of appearing and disappearing with the account list.
PROVIDERS: tuple[str, ...] = ("imap", "graph")

_STATUS_FIELD = {
    "active": "accounts_active",
    "error": "accounts_unreachable",
    "auth_error": "accounts_auth_rejected",
    "disabled": "accounts_disabled",
}


def _janitor_out(next_retire_at: datetime | None) -> JanitorOut:
    return JanitorOut(
        grace_days=settings.janitor_account_grace_days,
        action=settings.janitor_account_action,
        check_batch=settings.janitor_check_batch,
        token_retention_days=settings.janitor_token_retention_days,
        audit_retention_days=settings.janitor_audit_retention_days,
        next_retire_at=next_retire_at,
    )


@router.get("/stats/overview", response_model=OverviewOut, dependencies=[Depends(require_read)])
async def overview(principal: PrincipalDep, session: SessionDep) -> OverviewOut:
    """Headline counters for the dashboard."""
    account_ids = await readable_account_ids(session, principal)
    out = OverviewOut(providers=[ProviderStats(provider=name) for name in PROVIDERS])
    by_provider = {item.provider: item for item in out.providers}
    if not account_ids:
        return out

    buckets = (
        await session.execute(
            select(Account.provider, Account.status, func.count(Account.id))
            .where(Account.id.in_(account_ids))
            .group_by(Account.provider, Account.status)
        )
    ).all()
    for provider, status, count in buckets:
        row = by_provider.get(provider)
        if row is None:
            row = ProviderStats(provider=provider)
            by_provider[provider] = row
            out.providers.append(row)
        row.accounts_total += count
        out.accounts_total += count
        field = _STATUS_FIELD.get(status)
        if field is not None:
            setattr(row, field, getattr(row, field) + count)
            setattr(out, field, getattr(out, field) + count)

    per_provider_messages = (
        await session.execute(
            select(Account.provider, func.count(Message.id))
            .join(Message, Message.account_id == Account.id)
            .where(Account.id.in_(account_ids))
            .group_by(Account.provider)
        )
    ).all()
    for provider, count in per_provider_messages:
        out.messages_total += count
        row = by_provider.get(provider)
        if row is not None:
            row.messages = count

    out.unread_total = (
        await session.execute(
            select(func.count(MailboxMessage.id))
            .select_from(Folder)
            .join(MailboxMessage, MailboxMessage.folder_id == Folder.id)
            .where(
                Folder.account_id.in_(account_ids),
                Folder.role == "inbox",
                ~MailboxMessage.flag_seen,
            )
        )
    ).scalar_one()

    for account in await _accounts_with_status(session, account_ids, "auth_error"):
        if account_out(account).retire_warning:
            row = by_provider.get(account.provider)
            if row is not None:
                row.retire_warnings += 1

    return out


async def _accounts_with_status(
    session: SessionDep, account_ids: list[uuid.UUID], status: str
) -> list[Account]:
    return list(
        (
            await session.execute(
                select(Account)
                .where(Account.id.in_(account_ids), Account.status == status)
                # Oldest problem first: that is the one that has been ignored longest.
                .order_by(Account.updated_at)
            )
        )
        .scalars()
        .all()
    )


@router.get("/ops/summary", response_model=OpsSummaryOut, dependencies=[Depends(require_read)])
async def ops_summary(principal: PrincipalDep, session: SessionDep) -> OpsSummaryOut:
    """Accounts that need attention, split by failure mode, plus provider rate context."""
    account_ids = await readable_account_ids(session, principal)
    providers = await _provider_ops(session, account_ids)
    if not account_ids:
        return OpsSummaryOut(janitor=_janitor_out(None), providers=providers)

    unreachable = await accounts_out(
        session, await _accounts_with_status(session, account_ids, "error")
    )
    auth_rejected = await accounts_out(
        session, await _accounts_with_status(session, account_ids, "auth_error")
    )
    deadlines = [item.retire_at for item in auth_rejected if item.retire_at is not None]
    return OpsSummaryOut(
        unreachable=unreachable,
        auth_rejected=auth_rejected,
        janitor=_janitor_out(min(deadlines) if deadlines else None),
        providers=providers,
    )


async def _provider_ops(session: SessionDep, account_ids: list[uuid.UUID]) -> list[ProviderOps]:
    """Per-provider throughput over the recent window, next to the configured caps.

    Nothing persists throttle responses (the Graph client sleeps on `Retry-After` and
    moves on), so the counters here come from `sync_runs` and `outbox` — what actually ran
    and what actually went out.
    """
    rows = {
        name: ProviderOps(
            provider=name,
            send_limit_per_hour=settings.agent_send_limit_per_hour,
            max_connections_per_account=(
                settings.imap_max_conn_per_account if name == "imap" else None
            ),
            poll_interval_s=settings.graph_poll_interval_s if name == "graph" else None,
        )
        for name in PROVIDERS
    }
    if not account_ids:
        return list(rows.values())

    now = datetime.now(UTC)
    accounts = (
        await session.execute(
            select(Account.provider, func.count(Account.id))
            .where(Account.id.in_(account_ids))
            .group_by(Account.provider)
        )
    ).all()
    for provider, count in accounts:
        if provider in rows:
            rows[provider].accounts_total = count

    runs = (
        await session.execute(
            select(
                Account.provider,
                func.count(SyncRun.id),
                func.count(SyncRun.id).filter(SyncRun.error.isnot(None)),
            )
            .join(Account, Account.id == SyncRun.account_id)
            .where(Account.id.in_(account_ids), SyncRun.started_at >= now - timedelta(hours=24))
            .group_by(Account.provider)
        )
    ).all()
    for provider, total, failed in runs:
        if provider in rows:
            rows[provider].sync_runs_24h = total
            rows[provider].sync_failures_24h = failed

    sent = (
        await session.execute(
            select(Account.provider, func.count(Outbox.id))
            .join(Account, Account.id == Outbox.account_id)
            .where(
                Account.id.in_(account_ids),
                Outbox.status == "sent",
                Outbox.sent_at >= now - timedelta(hours=1),
            )
            .group_by(Account.provider)
        )
    ).all()
    for provider, count in sent:
        if provider in rows:
            rows[provider].sent_last_hour = count

    return list(rows.values())
