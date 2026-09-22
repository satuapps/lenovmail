# Lenovmail — authored by satuapps
"""Verify agent bearer tokens for the MCP server.

The accepted token is the same as the REST token (`lnv_...` in the `agent_tokens` table), so
scope, account limits, expiry, and revocation apply identically across both transports.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession

from ..api.deps import load_agent
from ..models import Account, AgentToken, User


@dataclass(slots=True)
class AgentPrincipal:
    """MCP caller: agent token + its owner. Denials use `ToolError`."""

    token: str
    agent: AgentToken
    user: User

    @property
    def scopes(self) -> frozenset[str]:
        return frozenset(self.agent.scopes or ())

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise ToolError(f"token lacks scope {scope}")

    def ensure_account(self, account_id: uuid.UUID) -> None:
        allowed = self.agent.account_ids
        if allowed and account_id not in allowed:
            raise ToolError("account is out of token scope")

    async def account(self, session: AsyncSession, account_id: uuid.UUID) -> Account:
        """Account owned by the token's owner and within its scope, or `ToolError`."""
        account = await session.get(Account, account_id)
        if account is None or account.owner_id != self.user.id:
            raise ToolError("account not found")
        self.ensure_account(account.id)
        return account


async def resolve_agent(session: AsyncSession, token: str) -> AgentPrincipal:
    """Token → principal, or `ToolError` if the token is invalid, expired, or revoked."""
    agent = await load_agent(session, token)
    if agent is None:
        raise ToolError("agent token is invalid, expired, or revoked")
    user = await session.get(User, agent.owner_id)
    if user is None or not user.is_active:
        raise ToolError("token owner is inactive")
    return AgentPrincipal(token=token, agent=agent, user=user)


class AgentTokenVerifier(TokenVerifier):
    """MCP `TokenVerifier`: validates agent tokens and touches `last_used_at`."""

    async def verify_token(self, token: str) -> AccessToken | None:
        from ..api.deps import touch_agent_last_used
        from ..db import SessionLocal

        async with SessionLocal() as session:
            agent = await load_agent(session, token)
            if agent is None:
                return None
            user = await session.get(User, agent.owner_id)
            if user is None or not user.is_active:
                return None
            await touch_agent_last_used(session, agent)
            return AccessToken(
                token=token,
                client_id=str(agent.id),
                scopes=list(agent.scopes or ()),
                subject=str(user.id),
                claims={"agent_token_id": str(agent.id), "user_id": str(user.id)},
            )
