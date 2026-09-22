# Lenovmail — authored by satuapps (satuapps.com)
"""FastAPI application: lifespan, middleware, GUI serving, and error handling.

The built GUI (`web/dist`) is served by the same process so the session cookie stays
same-origin: no CORS, no domain mismatch between the page and the API.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from .. import __version__
from ..config import settings
from ..db import dispose_engine
from ..logging import configure_logging, get_logger
from ..mcp.server import build_mcp_app, set_arq_pool
from ..sync.events import close_events, init_events
from .routes import api_router

log = get_logger(__name__)

# Vite development origin; production is served same-origin by this app.
DEV_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


def _static_dir() -> Path | None:
    """Built GUI directory: `/app/static` (Docker) or `web/dist` (local)."""
    candidates = (
        Path("/app/static"),
        Path(__file__).resolve().parents[3] / "web" / "dist",
    )
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate
    return None


def _path_targets(path: str) -> list[str]:
    """UUIDs that appear in the path — used as audit `target_ids`."""
    return [part for part in path.split("/") if len(part) == 36 and part.count("-") == 4]


async def _record_agent_call(request: Request, status_code: int) -> None:
    from .. import db
    from ..api.security import hash_agent_token
    from ..models import AgentAudit, AgentToken

    token = (request.headers.get("authorization") or "")[7:].strip()
    digest = hash_agent_token(token)
    outcome = "ok" if status_code < 400 else ("denied" if status_code in (401, 403) else "error")
    try:
        async with db.SessionLocal() as session:
            agent = (
                await session.execute(select(AgentToken).where(AgentToken.token_hash == digest))
            ).scalar_one_or_none()
            session.add(
                AgentAudit(
                    token_id=agent.id if agent else None,
                    user_id=agent.owner_id if agent else None,
                    tool=f"{request.method} {request.url.path}",
                    params_digest=hashlib.sha256(str(request.url.query).encode()).hexdigest()[:32],
                    target_ids=_path_targets(request.url.path) or None,
                    outcome=outcome,
                )
            )
            await session.commit()
    except Exception:  # noqa: BLE001 - audit must never fail the request
        log.warning("agent_audit_failed", path=request.url.path, exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    app.state.redis = init_events(settings.redis_url)
    app.state.arq = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    set_arq_pool(app.state.arq)
    log.info("api_started", version=__version__, static=bool(_static_dir()))
    try:
        # The MCP sub-application has its own lifespan (Streamable HTTP session
        # manager); the Starlette mount doesn't run it, so it's driven from here.
        async with app.state.mcp_app.router.lifespan_context(app.state.mcp_app):
            yield
    finally:
        set_arq_pool(None)
        await app.state.arq.aclose()
        await close_events()
        await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Lenovmail",
        version=__version__,
        summary="Multi-account IMAP + Microsoft Graph mail infra with a GUI and agent API",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(DEV_ORIGINS),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router)

    # MCP server for AI agents: /api/mcp (see `mcp/server.py`). Mounted after the REST
    # router so REST routes still win over this mount.
    mcp_app = build_mcp_app()
    app.state.mcp_app = mcp_app
    app.mount("/api", mcp_app)

    @app.middleware("http")
    async def audit_agent_calls(request: Request, call_next):
        """Record every agent-token API call into `agent_audit`.

        Done in middleware, not per-route, so no new route can forget to log it.
        A logging failure never fails the request.
        """
        response = await call_next(request)
        authorization = request.headers.get("authorization") or ""
        if not authorization.lower().startswith("bearer ") or not request.url.path.startswith(
            "/api"
        ):
            return response
        await _record_agent_call(request, response.status_code)
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        log.exception("unhandled_error", path=request.url.path)
        return JSONResponse({"detail": "internal error"}, status_code=500)

    static_dir = _static_dir()
    if static_dir is not None:
        assets = static_dir / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> FileResponse:
            """Serve the static file if it exists; otherwise index.html (client-side routing)."""
            candidate = (static_dir / path).resolve()
            if path and candidate.is_file() and static_dir in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(static_dir / "index.html")

    return app


app = create_app()
