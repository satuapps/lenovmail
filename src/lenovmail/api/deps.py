# Lenovmail — authored by satuapps (satuapps.com)
"""FastAPI dependencies: session/agent, account authorization, and Redis access.

Two kinds of caller are served:

* **Browser** — session cookie (`ln_session`) from login; every scope is allowed.
* **AI agent** — `Authorization: Bearer lnv_...`; scopes, account list, and send limit
  are constrained by the `agent_tokens` row, and every call is logged to `agent_audit`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any

import redis.asyncio as redis_async
from arq.connections import ArqRedis
from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Account, AgentToken, User
from . import security

# Known scopes. Browser sessions hold all of them.
ALL_SCOPES = frozenset({"mail.read", "mail.write", "mail.send", "mail.delete", "mail.manage"})


@dataclass(slots=True)
class Principal:
    """Caller identity plus its constraints."""

    user: User
    scopes: frozenset[str]
    agent: AgentToken | None = None
    account_ids: list[uuid.UUID] | None = None
    audit_params: dict[str, Any] = field(default_factory=dict)

    @property
    def is_agent(self) -> bool:
        return self.agent is not None

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.id

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail=f"token lacks scope {scope}")

    def ensure_account(self, account_id: uuid.UUID) -> None:
        if self.account_ids is not None and account_id not in self.account_ids:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="account outside token's scope")


def get_redis(request: Request) -> redis_async.Redis:
    client = getattr(request.app.state, "redis", None)
    if client is None:  # pragma: no cover - only if startup failed
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="Redis not ready")
    return client


def get_arq(request: Request) -> ArqRedis:
    pool = getattr(request.app.state, "arq", None)
    if pool is None:  # pragma: no cover - only if startup failed
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="job queue not ready")
    return pool


SessionDep = Annotated[AsyncSession, Depends(get_session)]
RedisDep = Annotated[redis_async.Redis, Depends(get_redis)]
ArqDep = Annotated[ArqRedis, Depends(get_arq)]


async def load_agent(session: AsyncSession, token: str) -> AgentToken | None:
    """Valid agent token row (not revoked, not expired), or None."""
    digest = security.hash_agent_token(token)
    row = (
        await session.execute(select(AgentToken).where(AgentToken.token_hash == digest))
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    if row.expires_at is not None and row.expires_at <= datetime.now(UTC):
        return None
    return row


async def touch_agent_last_used(session: AsyncSession, agent: AgentToken) -> None:
    """Record the token's last-used time (throttled to one write per minute per token)."""
    if agent.last_used_at is None or (datetime.now(UTC) - agent.last_used_at).total_seconds() > 60:
        agent.last_used_at = datetime.now(UTC)
        await session.commit()


async def current_principal(
    request: Request,
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Browser session or agent token; 401 if neither is present/valid."""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        agent = await load_agent(session, token)
        if agent is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid agent token")
        user = await session.get(User, agent.owner_id)
        if user is None or not user.is_active:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="token owner is inactive")
        await touch_agent_last_used(session, agent)
        return Principal(
            user=user,
            scopes=frozenset(agent.scopes or ()),
            agent=agent,
            account_ids=list(agent.account_ids) if agent.account_ids else None,
        )

    cookie = request.cookies.get(security.SESSION_COOKIE)
    if not cookie:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="not signed in")
    user_id = await security.read_session(get_redis(request), cookie)
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="session expired")
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="user is inactive")
    return Principal(user=user, scopes=ALL_SCOPES)


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def require_admin(principal: PrincipalDep) -> Principal:
    if principal.user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="admin role required")
    return principal


def require_read(principal: PrincipalDep) -> Principal:
    """Scope gate for routes that return mailbox content.

    Attached to the read routes themselves rather than to `owned_account`, which mutating
    routes also use: a `mail.send`-only token must still be able to queue a message without
    being able to read the mailbox.
    """
    principal.require("mail.read")
    return principal


ReaderDep = Annotated[Principal, Depends(require_read)]


AdminDep = Annotated[Principal, Depends(require_admin)]


async def owned_account(
    account_id: uuid.UUID, principal: PrincipalDep, session: SessionDep
) -> Account:
    """Account owned by the principal (or within the agent token's scope), 404 if not."""
    row = await session.get(Account, account_id)
    if row is None or row.owner_id != principal.user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="account not found")
    principal.ensure_account(row.id)
    return row


AccountDep = Annotated[Account, Depends(owned_account)]
