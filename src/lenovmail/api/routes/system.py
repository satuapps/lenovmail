# Lenovmail — authored by satuapps
"""Health check and application metadata."""

from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import text

from ... import __version__
from ..deps import SessionDep
from ..schemas import HealthOut

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/healthz", response_model=HealthOut)
async def healthz(request: Request, session: SessionDep) -> HealthOut:
    """Readiness status: used by the Docker healthcheck and the GUI to surface errors."""
    database = True
    try:
        await session.execute(text("select 1"))
    except Exception:
        database = False
    redis_ok = True
    redis = getattr(request.app.state, "redis", None)
    try:
        if redis is None:
            redis_ok = False
        else:
            await redis.ping()
    except Exception:
        redis_ok = False
    status = "ok" if database and redis_ok else "degraded"
    return HealthOut(status=status, database=database, redis=redis_ok, version=__version__)
