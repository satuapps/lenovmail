# Lenovmail — authored by satuapps
"""IMAP connection pool.

`IMAPClient` is blocking, so **every** call to it goes through `asyncio.to_thread`.
A single connection is used by only one coroutine at a time (guaranteed by the
semaphore + idle connection list), because `IMAPClient` objects are not safe for
concurrent use.

This is the only module allowed to import `imapclient`.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from imapclient import IMAPClient, SocketTimeout
from imapclient.exceptions import (
    IllegalStateError,
    IMAPClientAbortError,
    IMAPClientError,
    IMAPClientReadOnlyError,
    LoginError,
    ProtocolError,
)

from ..logging import get_logger

log = get_logger(__name__)

CONNECT_TIMEOUT_S = 10.0
READ_TIMEOUT_S = 60.0
ALLOWED_SECURITY = ("ssl", "starttls", "none")

# Capabilities that affect the sync path (stored in `imap_settings.caps`).
TRACKED_CAPABILITIES = {
    "condstore": b"CONDSTORE",
    "move": b"MOVE",
    "idle": b"IDLE",
    "special_use": b"SPECIAL-USE",
    "uidplus": b"UIDPLUS",
}


class MailAuthError(Exception):
    """Credentials rejected by the server. Must not be retried; account is marked `auth_error`."""


class MailTransientError(Exception):
    """Transient failure (network/timeout/server). Safe to retry with backoff."""


class MailPermanentError(Exception):
    """Request invalid for the server state (e.g. folder doesn't exist). Do not retry."""


@dataclass(slots=True)
class AccountImapConfig:
    host: str
    port: int
    security: str
    username: str
    password: str

    def fingerprint(self) -> tuple[str, int, str, str]:
        return (self.host, self.port, self.security, self.username)


def _connect_sync(cfg: AccountImapConfig) -> IMAPClient:
    """Open + authenticate a new connection. Called from a thread."""
    if cfg.security not in ALLOWED_SECURITY:
        raise MailPermanentError(f"unknown security mode: {cfg.security}")
    client = IMAPClient(
        cfg.host,
        cfg.port,
        ssl=cfg.security == "ssl",
        timeout=SocketTimeout(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
    )
    try:
        if cfg.security == "starttls":
            client.starttls()
        client.login(cfg.username, cfg.password)
    except BaseException:
        with contextlib.suppress(Exception):
            client.shutdown()
        raise
    return client


def detect_caps(client: IMAPClient) -> dict[str, bool]:
    """Capabilities reported by the server, in the shape stored in the DB."""
    try:
        caps = client.capabilities()
    except Exception:  # capabilities are only an optimization; failure is not fatal
        log.warning("capability_query_failed", exc_info=True)
        return dict.fromkeys(TRACKED_CAPABILITIES, False)
    upper = {c.upper() for c in caps}
    return {name: flag in upper for name, flag in TRACKED_CAPABILITIES.items()}


class ImapConn:
    """Thin async wrapper over `IMAPClient`."""

    def __init__(self, client: IMAPClient, caps: dict[str, bool] | None = None) -> None:
        self._client = client
        self.caps: dict[str, bool] = caps or {}

    @property
    def condstore(self) -> bool:
        return bool(self.caps.get("condstore"))

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self._client, method)
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except LoginError as exc:
            raise MailAuthError(str(exc) or "IMAP login rejected") from exc
        except (IMAPClientReadOnlyError, IllegalStateError) as exc:
            raise MailPermanentError(f"{method}: {exc}") from exc
        except (
            IMAPClientAbortError,
            IMAPClientError,
            ProtocolError,
            TimeoutError,
            OSError,
            ssl.SSLError,
        ) as exc:
            raise MailTransientError(f"{method}: {exc}") from exc

    # --- operations used by the sync layer ---------------------------------------
    async def noop(self) -> Any:
        return await self._call("noop")

    async def list_folders(self) -> list[tuple[tuple[bytes, ...], str, str]]:
        return await self._call("list_folders")

    async def select_folder(self, folder: str, readonly: bool = False) -> dict[bytes, Any]:
        return await self._call("select_folder", folder, readonly=readonly)

    async def unselect_folder(self) -> Any:
        return await self._call("unselect_folder")

    async def folder_status(self, folder: str, what: tuple[str, ...] | None = None) -> dict:
        return await self._call("folder_status", folder, what)

    async def search(self, criteria: Any) -> list[int]:
        return await self._call("search", criteria)

    async def fetch(self, messages: Any, data: Any, modifiers: Any = None) -> dict:
        return await self._call("fetch", messages, data, modifiers=modifiers)

    async def get_flags(self, messages: Any) -> dict:
        return await self._call("get_flags", messages)

    async def add_flags(self, messages: Any, flags: Any, silent: bool = False) -> Any:
        return await self._call("add_flags", messages, flags, silent=silent)

    async def remove_flags(self, messages: Any, flags: Any, silent: bool = False) -> Any:
        return await self._call("remove_flags", messages, flags, silent=silent)

    async def copy(self, messages: Any, folder: str) -> Any:
        return await self._call("copy", messages, folder)

    async def move(self, messages: Any, folder: str) -> Any:
        return await self._call("move", messages, folder)

    async def expunge(self, messages: Any = None) -> Any:
        return await self._call("expunge", messages)

    async def uid_expunge(self, messages: Any) -> Any:
        return await self._call("uid_expunge", messages)

    async def append(self, folder: str, msg: bytes, flags: tuple[str, ...] = ()) -> Any:
        return await self._call("append", folder, msg, flags=flags)

    async def idle(self) -> Any:
        return await self._call("idle")

    async def idle_check(self, wait_seconds: float | None = None) -> Any:
        """Wait for the IDLE response; `wait_seconds` bounds the blocking socket read."""
        return await self._call("idle_check", wait_seconds)

    async def idle_done(self) -> Any:
        return await self._call("idle_done")

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self._client.logout)


@dataclass
class ImapPool:
    """Connection pool for a single account.

    `on_caps` is called once whenever detected capabilities differ from what's known,
    so the sync layer can persist them to `imap_settings.caps`.
    """

    account_id: str
    config: AccountImapConfig
    max_connections: int = 3
    on_caps: Callable[[dict[str, bool]], Awaitable[None]] | None = None
    known_caps: dict[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._sem = asyncio.Semaphore(self.max_connections)
        self._idle: list[ImapConn] = []
        self._lock = asyncio.Lock()
        self._closed = False

    async def _new_connection(self) -> ImapConn:
        try:
            client = await asyncio.to_thread(_connect_sync, self.config)
        except LoginError as exc:
            raise MailAuthError(str(exc) or "IMAP login rejected") from exc
        except (IMAPClientAbortError, IMAPClientError, ProtocolError, TimeoutError, OSError) as exc:
            raise MailTransientError(f"IMAP connection failed: {exc}") from exc

        caps = await asyncio.to_thread(detect_caps, client)
        if caps.get("condstore"):
            with contextlib.suppress(Exception):
                await asyncio.to_thread(client.enable, "CONDSTORE")
        conn = ImapConn(client, caps)

        if caps != self.known_caps:
            self.known_caps = caps
            log.info("imap_caps_detected", account_id=self.account_id, caps=caps)
            if self.on_caps is not None:
                with contextlib.suppress(Exception):
                    await self.on_caps(caps)
        return conn

    async def _acquire(self) -> ImapConn:
        while True:
            async with self._lock:
                conn = self._idle.pop() if self._idle else None
            if conn is None:
                return await self._new_connection()
            try:
                # Servers drop idle connections (GreenMail immediately, Gmail after minutes).
                # Without this probe the next real command fails with a bare EOF and the whole
                # sync cycle is reported as an outage.
                await conn.noop()
            except (MailTransientError, MailPermanentError, OSError):
                await conn.close()
                continue
            return conn

    async def _release(self, conn: ImapConn) -> None:
        async with self._lock:
            if not self._closed and len(self._idle) < self.max_connections:
                self._idle.append(conn)
                return
        await conn.close()

    @contextlib.asynccontextmanager
    async def connection(self) -> AsyncIterator[ImapConn]:
        """Borrow one connection; return it to the pool if no error occurred."""
        async with self._sem:
            conn = await self._acquire()
            reusable = False
            try:
                yield conn
                reusable = True
            finally:
                if reusable:
                    await self._release(conn)
                else:
                    # Any error: the connection is discarded so protocol state isn't inherited.
                    await conn.close()

    async def open_idle_connection(self) -> ImapConn:
        """Dedicated IDLE connection, outside the pool quota (used by the idler process)."""
        return await self._new_connection()

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            conns, self._idle = self._idle, []
        for conn in conns:
            await conn.close()


_pools: dict[str, ImapPool] = {}
_pools_lock = asyncio.Lock()


async def get_pool(
    account_id: str,
    config: AccountImapConfig,
    *,
    max_connections: int = 3,
    on_caps: Callable[[dict[str, bool]], Awaitable[None]] | None = None,
) -> ImapPool:
    """Get (or create) the pool for an account; the pool is rebuilt if settings changed."""
    async with _pools_lock:
        existing = _pools.get(account_id)
        if existing is not None:
            if existing.config.fingerprint() == config.fingerprint():
                existing.on_caps = on_caps or existing.on_caps
                return existing
            await existing.close()
        pool = ImapPool(
            account_id=account_id,
            config=config,
            max_connections=max_connections,
            on_caps=on_caps,
        )
        _pools[account_id] = pool
        return pool


async def reset_pool(account_id: str) -> None:
    """Close and discard the account's pool (used after credentials change)."""
    async with _pools_lock:
        pool = _pools.pop(account_id, None)
    if pool is not None:
        await pool.close()


async def close_all_pools() -> None:
    async with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        await pool.close()
