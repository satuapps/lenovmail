# Lenovmail — authored by satuapps (satuapps.com)
"""AI agent tokens: scoped creation, listing, revocation, and auditing.

The token is shown only once, at creation time; what's stored in the database is its
sha256 hash, so a leaked table doesn't hand out access on its own.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from ...config import settings
from ...models import AgentAudit, AgentToken
from .. import security
from ..deps import ALL_SCOPES, PrincipalDep, SessionDep
from ..schemas import AgentAuditOut, AgentTokenCreate, AgentTokenCreated, AgentTokenOut

router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.get("/tokens", response_model=list[AgentTokenOut])
async def list_tokens(principal: PrincipalDep, session: SessionDep) -> list[AgentTokenOut]:
    rows = (
        await session.execute(
            select(AgentToken)
            .where(AgentToken.owner_id == principal.user_id)
            .order_by(AgentToken.created_at.desc())
        )
    ).scalars()
    return [AgentTokenOut.model_validate(row) for row in rows]


@router.post("/tokens", response_model=AgentTokenCreated, status_code=status.HTTP_201_CREATED)
async def create_token(
    body: AgentTokenCreate, principal: PrincipalDep, session: SessionDep
) -> AgentTokenCreated:
    unknown = set(body.scopes) - ALL_SCOPES
    if unknown:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"unknown scope: {sorted(unknown)}")
    if not body.scopes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="select at least one scope")
    if body.account_ids:
        from ...models import Account

        owned = set(
            (
                await session.execute(
                    select(Account.id).where(
                        Account.owner_id == principal.user_id, Account.id.in_(body.account_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        missing = [str(item) for item in body.account_ids if item not in owned]
        if missing:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail=f"accounts not owned by you: {missing}"
            )

    token, digest = security.new_agent_token()
    row = AgentToken(
        owner_id=principal.user_id,
        name=body.name,
        token_hash=digest,
        scopes=list(body.scopes),
        account_ids=list(body.account_ids) if body.account_ids else None,
        require_send_approval=body.require_send_approval,
        send_limit_per_hour=(
            body.send_limit_per_hour
            if body.send_limit_per_hour is not None
            else settings.agent_send_limit_per_hour
        ),
        expires_at=(
            datetime.now(UTC) + timedelta(days=body.expires_in_days)
            if body.expires_in_days
            else None
        ),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    # `token` only exists here (not a column), so it's filled in after ORM validation.
    return AgentTokenCreated(**AgentTokenOut.model_validate(row).model_dump(), token=token)


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_token(token_id: uuid.UUID, principal: PrincipalDep, session: SessionDep) -> None:
    """Revoke the token (records `revoked_at` rather than deleting it, to keep the audit
    trail intact)."""
    row = await session.get(AgentToken, token_id)
    if row is None or row.owner_id != principal.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="token not found")
    row.revoked_at = datetime.now(UTC)
    await session.commit()


@router.get("/audit", response_model=list[AgentAuditOut])
async def list_audit(
    principal: PrincipalDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AgentAuditOut]:
    rows = (
        await session.execute(
            select(AgentAudit)
            .where(AgentAudit.user_id == principal.user_id)
            .order_by(AgentAudit.created_at.desc())
            .limit(limit)
        )
    ).scalars()
    return [AgentAuditOut.model_validate(row) for row in rows]
