# Lenovmail — authored by satuapps (satuapps.com)
"""Audit rows for REST actions that need a trail regardless of who made them.

The `audit_agent_calls` middleware in `app.py` only records calls carrying an agent
token, which is the right default: a browser session is already the user, and logging
every GUI request would bury the interesting rows. Actions that hand over secret material
are the exception — they are recorded here for sessions and tokens alike.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from ..logging import get_logger
from ..models import AgentAudit
from .deps import Principal

log = get_logger(__name__)


async def record(
    session: AsyncSession,
    principal: Principal,
    tool: str,
    *,
    target_ids: Sequence[str] | None = None,
    outcome: str = "ok",
    error: str | None = None,
) -> None:
    """Write one `agent_audit` row. Never raises: auditing must not fail the request."""
    try:
        session.add(
            AgentAudit(
                token_id=principal.agent.id if principal.agent else None,
                user_id=principal.user_id,
                tool=tool,
                target_ids=list(target_ids) if target_ids else None,
                outcome=outcome,
                error=error,
            )
        )
        await session.commit()
    except Exception:  # noqa: BLE001 - audit must never fail the request
        log.warning("audit_failed", tool=tool, exc_info=True)
