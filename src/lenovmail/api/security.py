# Lenovmail — authored by satuapps
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
from datetime import timedelta

import redis.asyncio as redis_async
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from ..config import settings
from ..redis_helpers import resolved

SESSION_COOKIE = "ln_session"
SESSION_PREFIX = "session:"
SESSION_INDEX_PREFIX = "sessions:user:"
AGENT_TOKEN_PREFIX = "lnv_"

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


async def store_session(redis: redis_async.Redis, user_id: uuid.UUID, token: str) -> None:
    ttl = _session_ttl()
    async with redis.pipeline(transaction=True) as pipe:
        pipe.set(f"{SESSION_PREFIX}{token}", str(user_id), ex=ttl)
        pipe.sadd(f"{SESSION_INDEX_PREFIX}{user_id}", token)
        # The index expires too, so old token sets don't pile up forever.
        pipe.expire(f"{SESSION_INDEX_PREFIX}{user_id}", ttl)
        await pipe.execute()


async def read_session(redis: redis_async.Redis, token: str) -> uuid.UUID | None:
    value = await resolved(redis.get(f"{SESSION_PREFIX}{token}"))
    if not value:
        return None
    try:
        return uuid.UUID(value if isinstance(value, str) else value.decode())
    except ValueError:
        return None


async def drop_session(redis: redis_async.Redis, token: str, user_id: uuid.UUID | None) -> None:
    await resolved(redis.delete(f"{SESSION_PREFIX}{token}"))
    if user_id is not None:
        await resolved(redis.srem(f"{SESSION_INDEX_PREFIX}{user_id}", token))


async def drop_user_sessions(redis: redis_async.Redis, user_id: uuid.UUID) -> int:
    """Revoke all of a user's sessions (used on password change or account deactivation)."""
    index_key = f"{SESSION_INDEX_PREFIX}{user_id}"
    tokens = await resolved(redis.smembers(index_key))
    keys = [f"{SESSION_PREFIX}{token}" for token in tokens]
    if keys:
        await resolved(redis.delete(*keys))
    await resolved(redis.delete(index_key))
    return len(keys)


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
