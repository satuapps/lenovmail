# Lenovmail — authored by satuapps
"""Bridge between `accounts`/`imap_settings` rows and the provider connectors.

Shared by the sync engine, idler, API, and mail sender so that credential decryption
and capability storage live in exactly one place.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..crypto import account_aad, decrypt
from ..logging import get_logger
from ..models import Account, GraphSettings, ImapSettings
from .graph import GraphClient, dump_token_cache
from .imap_pool import AccountImapConfig, ImapPool, get_pool

log = get_logger(__name__)


class AccountConfigError(Exception):
    """Account is missing the provider settings required for the requested operation."""


async def get_imap_settings(session: AsyncSession, account_id: uuid.UUID) -> ImapSettings:
    row = await session.get(ImapSettings, account_id)
    if row is None:
        raise AccountConfigError(f"account {account_id} has no imap_settings")
    return row


async def get_graph_settings(session: AsyncSession, account_id: uuid.UUID) -> GraphSettings:
    row = await session.get(GraphSettings, account_id)
    if row is None:
        raise AccountConfigError(f"account {account_id} has no graph_settings")
    return row


def _password(settings_row: ImapSettings, field: str, account_id: uuid.UUID) -> str:
    blob = getattr(settings_row, field)
    if not blob:
        raise AccountConfigError(f"credential {field} for account {account_id} is not set")
    return decrypt(account_aad(account_id, field), blob).decode()


async def load_imap_config(session: AsyncSession, account: Account) -> AccountImapConfig:
    """IMAP connection settings; the password is decrypted on each call (not cached)."""
    row = await get_imap_settings(session, account.id)
    return AccountImapConfig(
        host=row.host,
        port=row.port,
        security=row.security,
        username=row.username,
        password=_password(row, "password_enc", account.id),
    )


async def load_smtp_config(session: AsyncSession, account: Account) -> dict[str, object]:
    """SMTP settings, or raises `AccountConfigError` if the account cannot send."""
    row = await get_imap_settings(session, account.id)
    if not row.smtp_host or not row.smtp_port:
        raise AccountConfigError(f"account {account.id} has no SMTP settings")
    return {
        "host": row.smtp_host,
        "port": row.smtp_port,
        "security": row.smtp_security or "starttls",
        "username": row.smtp_username or row.username,
        "password": _password(row, "smtp_password_enc", account.id)
        if row.smtp_password_enc
        else _password(row, "password_enc", account.id),
        "append_to_sent": row.append_to_sent,
    }


SessionFactory = Callable[[], AsyncSession]


def caps_saver(
    session_factory: SessionFactory, account_id: uuid.UUID
) -> Callable[[dict[str, bool]], Awaitable[None]]:
    """`on_caps` callback that persists detected capabilities to `imap_settings.caps`."""

    async def save(caps: dict[str, bool]) -> None:
        async with session_factory() as session:
            await session.execute(
                update(ImapSettings).where(ImapSettings.account_id == account_id).values(caps=caps)
            )
            await session.commit()

    return save


async def get_account_pool(
    session: AsyncSession,
    account: Account,
    *,
    session_factory: SessionFactory | None = None,
) -> ImapPool:
    """IMAP pool for the account, with a capability saver when `session_factory` is given."""
    config = await load_imap_config(session, account)
    on_caps = caps_saver(session_factory, account.id) if session_factory is not None else None
    return await get_pool(
        str(account.id),
        config,
        max_connections=settings.imap_max_conn_per_account,
        on_caps=on_caps,
    )


async def mark_account_status(
    session: AsyncSession, account_id: uuid.UUID, status: str, detail: str | None = None
) -> None:
    """Update the account status (`active`, `auth_error`, `error`, `disabled`).

    Going back to `active` also clears `invalid_since`: the credentials just proved they
    work, so the janitor's grace period starts from scratch the next time they fail.
    """
    values: dict[str, object | None] = {
        "status": status,
        "status_detail": detail[:2000] if detail else None,
    }
    if status == "active":
        values["invalid_since"] = None
    await session.execute(update(Account).where(Account.id == account_id).values(**values))
    await session.commit()


async def stamp_last_sync(session: AsyncSession, account_id: uuid.UUID) -> None:
    await session.execute(
        update(Account).where(Account.id == account_id).values(last_sync_at=datetime.now(UTC))
    )
    await session.commit()


async def account_for_owner(
    session: AsyncSession, owner_id: uuid.UUID, email_address: str
) -> Account | None:
    return (
        await session.execute(
            select(Account).where(
                Account.owner_id == owner_id,
                Account.email_address == email_address.strip().lower(),
            )
        )
    ).scalar_one_or_none()


async def graph_client_for(session: AsyncSession, account: Account) -> GraphClient:
    """Ready-to-use Graph client for the account, loading the encrypted token cache from the DB."""
    settings_row = await get_graph_settings(session, account.id)
    return GraphClient.from_stored(
        account.id, settings_row.token_cache_enc, settings_row.home_account_id
    )


async def save_graph_tokens(
    session: AsyncSession, account_id: uuid.UUID, client: GraphClient
) -> None:
    """Persist the refreshed token cache.

    MSAL only sets `has_state_changed` when a new token is issued; without this save,
    a newly issued refresh token would be lost and the account would need reauthorization.
    """
    blob = dump_token_cache(account_id, client.token_cache)
    if blob is None:
        return
    settings_row = await get_graph_settings(session, account_id)
    settings_row.token_cache_enc = blob
    await session.commit()
