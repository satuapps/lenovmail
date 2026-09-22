# Lenovmail — authored by satuapps (satuapps.com)
"""Authentication: argon2 passwords, browser sessions in Redis, and hashed agent tokens.

Sessions are stored in Redis (not a signed cookie) so they can be revoked: logout,
password change, or deleting a user immediately kills every session. The cookie value is
a random 32-byte token — it carries no user data, so nothing leaks if the cookie is
copied, and the database needs no new session table.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import redis.asyncio as redis_async
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from ..config import settings
from ..redis_helpers import resolved

SESSION_COOKIE = "ln_session"
SESSION_PREFIX = "session:"
SESSION_INDEX_PREFIX = "sessions:user:"
SESSION_META_PREFIX = "session:meta:"
AGENT_TOKEN_PREFIX = "lnv_"

# How stale `last_seen_at` may get before a request rewrites it: without the throttle,
# every authenticated request would write to Redis.
SESSION_TOUCH_AFTER_S = 60
USER_AGENT_MAX_CHARS = 200

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def new_session_token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def _session_ttl() -> timedelta:
    return timedelta(days=max(settings.session_ttl_days, 1))


def session_public_id(token: str) -> str:
    """Stable handle for a session that is safe to send to the browser.

    The raw token is the credential; the API only ever exposes this digest of it, so a
    session list cannot be replayed as a login.
    """
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _meta_payload(
    user_id: uuid.UUID, *, ip: str | None, user_agent: str | None, now: datetime
) -> dict[str, str]:
    stamp = now.isoformat()
    return {
        "user_id": str(user_id),
        "created_at": stamp,
        "last_seen_at": stamp,
        "ip": ip or "",
        "user_agent": (user_agent or "")[:USER_AGENT_MAX_CHARS],
    }


async def store_session(
    redis: redis_async.Redis,
    user_id: uuid.UUID,
    token: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    ttl = _session_ttl()
    meta = _meta_payload(user_id, ip=ip, user_agent=user_agent, now=datetime.now(UTC))
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(f"{SESSION_PREFIX}{token}", str(user_id), ex=ttl)
        pipe.sadd(f"{SESSION_INDEX_PREFIX}{user_id}", token)
        # The index expires too, so old token sets don't pile up forever.
        pipe.expire(f"{SESSION_INDEX_PREFIX}{user_id}", ttl)
        pipe.hset(f"{SESSION_META_PREFIX}{token}", mapping=meta)
        pipe.expire(f"{SESSION_META_PREFIX}{token}", ttl)
        await pipe.execute()


async def read_session(redis: redis_async.Redis, token: str) -> uuid.UUID | None:
    value = await resolved(redis.get(f"{SESSION_PREFIX}{token}"))
    if not value:
        return None
    try:
        return uuid.UUID(value if isinstance(value, str) else value.decode())
    except ValueError:
        return None


async def touch_session(redis: redis_async.Redis, token: str, user_id: uuid.UUID) -> None:
    """Record that the session was used, at most once per `SESSION_TOUCH_AFTER_S`.

    Also fills in metadata for sessions that were created before it was recorded, so a
    live session shows up in the session list after its next request instead of never.
    """
    key = f"{SESSION_META_PREFIX}{token}"
    now = datetime.now(UTC)
    meta = await resolved(redis.hgetall(key))
    if not meta:
        payload = _meta_payload(user_id, ip=None, user_agent=None, now=now)
        await resolved(redis.hset(key, mapping=payload))
        await resolved(redis.expire(key, _session_ttl()))
        return
    last_seen = _parse_stamp(meta.get("last_seen_at"))
    if last_seen is not None and (now - last_seen).total_seconds() < SESSION_TOUCH_AFTER_S:
        return
    await resolved(redis.hset(key, "last_seen_at", now.isoformat()))


def _parse_stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def list_sessions(redis: redis_async.Redis, user_id: uuid.UUID) -> list[dict[str, object]]:
    """Live sessions for one user, newest first.

    Expired tokens are pruned from the index on the way past: Redis drops the session key
    on its own, but set members outlive it.
    """
    index_key = f"{SESSION_INDEX_PREFIX}{user_id}"
    tokens = await resolved(redis.smembers(index_key))
    out: list[dict[str, object]] = []
    for token in tokens:
        if not await resolved(redis.exists(f"{SESSION_PREFIX}{token}")):
            await resolved(redis.srem(index_key, token))
            await resolved(redis.delete(f"{SESSION_META_PREFIX}{token}"))
            continue
        meta = await resolved(redis.hgetall(f"{SESSION_META_PREFIX}{token}"))
        created_at = _parse_stamp(meta.get("created_at")) if meta else None
        if created_at is None:
            # No metadata yet; `touch_session` writes it on the session's next request.
            continue
        out.append(
            {
                "id": session_public_id(token),
                "created_at": created_at,
                "last_seen_at": _parse_stamp(meta.get("last_seen_at")),
                "ip": meta.get("ip") or None,
                "user_agent": meta.get("user_agent") or None,
            }
        )
    out.sort(key=lambda item: item["created_at"], reverse=True)  # type: ignore[arg-type,return-value]
    return out


async def drop_session(redis: redis_async.Redis, token: str, user_id: uuid.UUID | None) -> None:
    await resolved(redis.delete(f"{SESSION_PREFIX}{token}", f"{SESSION_META_PREFIX}{token}"))
    if user_id is not None:
        await resolved(redis.srem(f"{SESSION_INDEX_PREFIX}{user_id}", token))


async def revoke_session(redis: redis_async.Redis, user_id: uuid.UUID, session_id: str) -> bool:
    """Delete the session whose public id matches. `False` when the user has no such session."""
    index_key = f"{SESSION_INDEX_PREFIX}{user_id}"
    for token in await resolved(redis.smembers(index_key)):
        if session_public_id(token) != session_id:
            continue
        await drop_session(redis, token, user_id)
        return True
    return False


async def drop_user_sessions(redis: redis_async.Redis, user_id: uuid.UUID) -> int:
    """Revoke all of a user's sessions (used on password change or account deactivation)."""
    index_key = f"{SESSION_INDEX_PREFIX}{user_id}"
    tokens = await resolved(redis.smembers(index_key))
    keys = [f"{SESSION_PREFIX}{token}" for token in tokens]
    keys += [f"{SESSION_META_PREFIX}{token}" for token in tokens]
    if keys:
        await resolved(redis.delete(*keys))
    await resolved(redis.delete(index_key))
    return len(tokens)


def new_agent_token() -> tuple[str, bytes]:
    """Return (plaintext token shown once, sha256 hash to store)."""
    raw = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    token = f"{AGENT_TOKEN_PREFIX}{raw}"
    return token, hash_agent_token(token)


def hash_agent_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def token_prefix(token: str) -> str:
    """Leading slice of the token, shown in the agent token list."""
    return token[:12]
