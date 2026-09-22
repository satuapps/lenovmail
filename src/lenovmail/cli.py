# Lenovmail — authored by satuapps
"""Lenovmail operational commands: bootstrap, config discovery, manual sync, serve.

Used for things that don't have a place in the GUI: creating the first user, checking
auto-discovery results from the terminal, running one sync cycle without a worker, and
running the janitor on demand.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from datetime import UTC, datetime, timedelta

import typer
from sqlalchemy import select

from . import __version__
from .autoconfig.discover import discover as discover_email
from .config import settings
from .db import SessionLocal, dispose_engine
from .logging import configure_logging
from .maintenance import RetireAction, run_sweep
from .models import Account, AgentToken, User

app = typer.Typer(add_completion=False, help="Lenovmail — multi-mail IMAP + Graph infra.")


def _run(coro):
    return asyncio.run(coro)


@app.command("version")
def version() -> None:
    """Show the version."""
    typer.echo(__version__)


@app.command("bootstrap-admin")
def bootstrap_admin(
    email: str = typer.Option(..., help="Admin login email."),
    password: str | None = typer.Option(None, help="Leave empty to auto-generate."),
) -> None:
    """Create (or update) the first admin user, then print the password once."""
    from .api import security

    async def run() -> None:
        async with SessionLocal() as session:
            generated = password is None
            secret = password or secrets.token_urlsafe(18)
            user = (
                await session.execute(select(User).where(User.email == email.strip().lower()))
            ).scalar_one_or_none()
            if user is None:
                session.add(
                    User(
                        email=email.strip().lower(),
                        password_hash=security.hash_password(secret),
                        role="admin",
                    )
                )
            else:
                user.password_hash = security.hash_password(secret)
                user.role = "admin"
                user.is_active = True
            await session.commit()
        typer.echo(f"admin ready: {email.strip().lower()}")
        if generated:
            typer.echo(f"password: {secret}")

    _run(run())


@app.command("discover")
def discover_cmd(email: str) -> None:
    """Run the auto-discovery chain for an email address and print the result."""
    configure_logging()

    async def run() -> None:
        async with SessionLocal() as session:
            result = await discover_email(email.strip().lower(), session)
        typer.echo(json.dumps(result.to_dict(), indent=2))
        await dispose_engine()

    _run(run())


@app.command("sync")
def sync_cmd(
    email: str = typer.Option(..., help="Email address of the account to sync."),
    owner: str = typer.Option("", help="Email of the account owner (default: all owners)."),
    bodies: bool = typer.Option(True, help="Also download bodies that are missing."),
) -> None:
    """Run one sync cycle for an account (without a worker/queue)."""
    configure_logging()

    async def run() -> None:
        from .sync.events import close_events, init_events
        from .workers.tasks import sync_account

        init_events(settings.redis_url)
        async with SessionLocal() as session:
            query = select(Account).where(Account.email_address == email.strip().lower())
            if owner:
                user = (
                    await session.execute(select(User).where(User.email == owner.strip().lower()))
                ).scalar_one_or_none()
                if user is None:
                    typer.echo(f"owner not found: {owner}")
                    raise typer.Exit(code=1)
                query = query.where(Account.owner_id == user.id)
            account = (await session.execute(query)).scalar_one_or_none()
            if account is None:
                typer.echo(f"account not found: {email}")
                raise typer.Exit(code=1)
            account_id = account.id

        result = await sync_account({"bodies": bodies}, str(account_id))
        typer.echo(json.dumps(result, indent=2, default=str))
        await close_events()
        await dispose_engine()

    _run(run())


@app.command("agent-token")
def agent_token_cmd(
    email: str = typer.Option(..., help="Email of the token owner."),
    name: str = typer.Option("agent", help="Token name (for audit)."),
    scopes: str = typer.Option("mail.read", help="Comma-separated scopes."),
    days: int = typer.Option(90, help="Lifetime (days)."),
) -> None:
    """Issue an agent token for an account owner; the token is shown once."""
    from .api import security

    async def run() -> None:
        async with SessionLocal() as session:
            user = (
                await session.execute(select(User).where(User.email == email.strip().lower()))
            ).scalar_one_or_none()
            if user is None:
                typer.echo(f"user not found: {email}")
                raise typer.Exit(code=1)
            token, digest = security.new_agent_token()
            session.add(
                AgentToken(
                    owner_id=user.id,
                    name=name,
                    token_hash=digest,
                    scopes=[item.strip() for item in scopes.split(",") if item.strip()],
                    expires_at=datetime.now(UTC) + timedelta(days=days),
                )
            )
            await session.commit()
        typer.echo(token)
        await dispose_engine()

    _run(run())


@app.command("serve")
def serve_cmd(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8080),
    reload: bool = typer.Option(False, help="Auto-reload for development."),
) -> None:
    """Run the API + GUI (uvicorn)."""
    import uvicorn

    uvicorn.run("lenovmail.api.app:app", host=host, port=port, reload=reload)


@app.command("show-account")
def show_account(email: str) -> None:
    """Summarize the state of one account: folders, message counts, last sync cycle."""
    configure_logging()

    async def run() -> None:
        from sqlalchemy import func

        from .models import Folder, MailboxMessage, Message, SyncRun

        async with SessionLocal() as session:
            account = (
                await session.execute(
                    select(Account).where(Account.email_address == email.strip().lower())
                )
            ).scalar_one_or_none()
            if account is None:
                typer.echo(f"account not found: {email}")
                raise typer.Exit(code=1)
            folders = (
                await session.execute(
                    select(
                        Folder.name,
                        Folder.role,
                        Folder.sync_state,
                        Folder.last_synced_at,
                        func.count(MailboxMessage.id),
                    )
                    .join(MailboxMessage, MailboxMessage.folder_id == Folder.id, isouter=True)
                    .where(Folder.account_id == account.id)
                    .group_by(Folder.id)
                    .order_by(Folder.name)
                )
            ).all()
            total = (
                await session.execute(
                    select(func.count(Message.id)).where(Message.account_id == account.id)
                )
            ).scalar_one()
            runs = (
                await session.execute(
                    select(SyncRun.kind, SyncRun.added, SyncRun.finished_at)
                    .where(SyncRun.account_id == account.id)
                    .order_by(SyncRun.started_at.desc())
                    .limit(3)
                )
            ).all()
        typer.echo(
            f"{account.email_address} ({account.provider}) status={account.status} "
            f"messages={total} last_sync={account.last_sync_at}"
        )
        for folder in folders:
            typer.echo(f"  {folder[0]:<24} role={folder[1]:<8} {folder[2]:<6} messages={folder[4]}")
        for run in runs:
            typer.echo(f"  sync {run[0]} added={run[1]} finished={run[2]}")
        await dispose_engine()

    _run(run())


@app.command("janitor")
def janitor_cmd(
    action: str | None = typer.Option(
        None,
        "--action",
        help="Override how a retired account is handled: disable or delete.",
    ),
) -> None:
    """Run one janitor pass: re-check quarantined accounts, purge dead tokens and audit rows."""
    configure_logging()
    if action is not None and action not in ("disable", "delete"):
        typer.echo("--action must be 'disable' or 'delete'")
        raise typer.Exit(code=2)
    retire: RetireAction | None = "delete" if action == "delete" else None
    if action == "disable":
        retire = "disable"

    async def run() -> None:
        async with SessionLocal() as session:
            report = await run_sweep(session, action=retire)
        for key, value in report.as_dict().items():
            typer.echo(f"{key:<18} {value}")
        await dispose_engine()

    _run(run())


if __name__ == "__main__":  # pragma: no cover
    app()
