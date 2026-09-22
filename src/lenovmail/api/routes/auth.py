# Lenovmail — authored by satuapps (satuapps.com)
"""Browser session login: argon2, HttpOnly cookie, failed-attempt rate limiting, and the
session list a user can revoke from.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import func, select

from ...config import settings
from ...models import User
from ...redis_helpers import resolved
from .. import security
from ..deps import PrincipalDep, RedisDep, SessionDep
from ..schemas import LoginRequest, PasswordChange, SessionOut, UserOut

router = APIRouter(prefix="/api/auth", tags=["auth"])

LOGIN_FAIL_LIMIT = 10
LOGIN_FAIL_WINDOW_S = 900


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        security.SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_days * 86_400,
        httponly=True,
        samesite="lax",
        secure=settings.cookies_secure,
        path="/",
    )


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/login", response_model=UserOut)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    redis: RedisDep,
) -> UserOut:
    email = body.email.strip().lower()
    fail_key = f"login:fail:{email}"
    failures = await resolved(redis.get(fail_key))
    if failures is not None and int(failures) >= LOGIN_FAIL_LIMIT:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many failed attempts; try again later",
        )

    user = (
        await session.execute(select(User).where(func.lower(User.email) == email))
    ).scalar_one_or_none()
    if (
        user is None
        or not user.is_active
        or not security.verify_password(user.password_hash, body.password)
    ):
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(fail_key)
            pipe.expire(fail_key, LOGIN_FAIL_WINDOW_S)
            await pipe.execute()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid email or password")

    await resolved(redis.delete(fail_key))
    if security.password_needs_rehash(user.password_hash):
        user.password_hash = security.hash_password(body.password)
        await session.commit()

    token = security.new_session_token()
    await security.store_session(
        redis,
        user.id,
        token,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_session_cookie(response, token)
    return UserOut.model_validate(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request, response: Response, redis: RedisDep, principal: PrincipalDep
) -> None:
    """Delete the session in Redis and its cookie; safe to call repeatedly."""
    token = request.cookies.get(security.SESSION_COOKIE)
    if token:
        await security.drop_session(redis, token, principal.user.id)
    response.delete_cookie(security.SESSION_COOKIE, path="/")


@router.get("/me", response_model=UserOut)
async def me(principal: PrincipalDep) -> UserOut:
    return UserOut.model_validate(principal.user)


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(
    request: Request, redis: RedisDep, principal: PrincipalDep
) -> list[SessionOut]:
    """Every live browser session for the caller, newest first.

    Agent tokens are listed and revoked under `/api/agent/tokens`; this route is about the
    cookie sessions, so it refuses a token caller rather than returning an empty list.
    """
    if principal.is_agent:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="browser session required")
    cookie = request.cookies.get(security.SESSION_COOKIE)
    current_id = security.session_public_id(cookie) if cookie else None
    rows = await security.list_sessions(redis, principal.user_id)
    return [
        SessionOut(
            id=str(row["id"]),
            created_at=row["created_at"],  # type: ignore[arg-type]
            last_seen_at=row["last_seen_at"],  # type: ignore[arg-type]
            ip=row["ip"],  # type: ignore[arg-type]
            user_agent=row["user_agent"],  # type: ignore[arg-type]
            current=row["id"] == current_id,
        )
        for row in rows
    ]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(session_id: str, redis: RedisDep, principal: PrincipalDep) -> None:
    """Revoke one session. Revoking the caller's own signs this browser out on its next call."""
    if principal.is_agent:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="browser session required")
    if not await security.revoke_session(redis, principal.user_id, session_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="session not found")


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: PasswordChange,
    request: Request,
    response: Response,
    session: SessionDep,
    redis: RedisDep,
    principal: PrincipalDep,
) -> None:
    """Change the password, then revoke every session — including the caller's, which
    is replaced with a new one."""
    user = principal.user
    if not security.verify_password(user.password_hash, body.current_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="current password is incorrect")
    user.password_hash = security.hash_password(body.new_password)
    await session.commit()
    await security.drop_user_sessions(redis, user.id)
    token = security.new_session_token()
    await security.store_session(
        redis,
        user.id,
        token,
        ip=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_session_cookie(response, token)
