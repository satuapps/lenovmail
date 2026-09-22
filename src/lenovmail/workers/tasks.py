# Lenovmail — authored by satuapps (satuapps.com)
"""arq jobs: account sync (IMAP + Graph), body backfill, and IMAP IDLE monitoring.

`sync_account` runs one full cycle (header pass over all folders + body backfill) for
one account. A failure on one folder must not cancel the others — only
`MailAuthError`/`GraphAuthError`/`AccountConfigError` stop the account cycle (credentials
are no longer valid); transient failures are logged and the cycle continues to the next
folder.

`idle_watch` is a long-lived job (run with its own timeout, see `main.py`): it holds one
IMAP connection in IDLE mode for the account's inbox folder, and refreshes the Redis key
`idle:watch:<account_id>` (short TTL) every round so `ensure_idle_watchers` knows the
watcher is still alive and does not enqueue a duplicate.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import maintenance
from ..config import settings
from ..db import SessionLocal
from ..logging import get_logger
from ..models import Account, Folder, GraphSettings
from ..providers.accounts import (
    AccountConfigError,
    get_account_pool,
    get_graph_settings,
    mark_account_status,
    stamp_last_sync,
)
from ..providers.graph import GraphAuthError, GraphClient, GraphTransientError, dump_token_cache
from ..providers.imap_pool import ImapPool, MailAuthError, MailTransientError
from ..sync import graph_sync, imap_sync, outbox

log = get_logger(__name__)

IDLE_WATCH_LOCK_PREFIX = "idle:watch:"
IDLE_WATCH_LOCK_TTL_S = 90
IDLE_RECONNECT_BACKOFFS_S = (5, 10, 20, 40, 60)

# `sync:lock:` marks a cycle that is running, `sync:queued:` one that is waiting in the queue.
# Both expire on their own so a worker killed mid-cycle cannot lock an account out for good.
SYNC_LOCK_PREFIX = "sync:lock:"
SYNC_LOCK_TTL_S = 900
SYNC_QUEUED_PREFIX = "sync:queued:"
SYNC_QUEUED_TTL_S = 900

_IDLE_CHANGE_TOKENS = {"EXISTS", "RECENT", "FETCH"}
# imapclient raises this when the server announces a change before acknowledging IDLE.
_IDLE_HANDSHAKE_RACE = "Unexpected IDLE response"


def _idle_lock_key(account_id: str) -> str:
    return f"{IDLE_WATCH_LOCK_PREFIX}{account_id}"


async def _hold_idle_lock(redis: Any, lock_key: str) -> None:
    """Keep a watcher's liveness key alive for as long as the watcher runs.

    `idle_check` blocks for up to `idle_renew_s` (default 25 minutes), far longer than the
    key's TTL. Refreshing only around that call would let the key lapse mid-wait, and
    `ensure_idle_watchers` would start a second watcher for an account that already has one.
    The short TTL is deliberate: it is also what lets a crashed watcher be replaced quickly.
    """
    while True:
        await redis.set(lock_key, "1", ex=IDLE_WATCH_LOCK_TTL_S)
        await asyncio.sleep(IDLE_WATCH_LOCK_TTL_S / 3)


def _idle_has_changes(response: list[tuple]) -> bool:
    """`True` if the `idle_check` response contains EXISTS/RECENT/FETCH (new/changed message)."""
    for item in response:
        for part in item:
            if isinstance(part, bytes) and part.decode("ascii", "replace").upper() in (
                _IDLE_CHANGE_TOKENS
            ):
                return True
    return False


# --- sync_account: IMAP path -----------------------------------------------------------


async def _sync_imap_account(session: AsyncSession, account: Account) -> dict:
    # `sync_folder`/`fetch_bodies` roll back on failure, and a rollback makes the ORM
    # instance in this session stale. Reading its attributes afterward triggers a
    # synchronous lazy-load inside async code (MissingGreenlet). Because of this: identity
    # is stored as a plain value, and the account/folder are reloaded from the session on
    # every iteration.
    account_uuid = account.id
    account_id = str(account_uuid)
    try:
        pool = await get_account_pool(session, account, session_factory=SessionLocal)
    except (MailAuthError, AccountConfigError) as exc:
        await mark_account_status(session, account_uuid, "auth_error", str(exc))
        return {"account_id": account_id, "folders": 0, "added": 0, "bodies": 0, "error": str(exc)}
    except MailTransientError as exc:
        # Server unreachable: log as a transient error, not a job failure.
        await mark_account_status(session, account_uuid, "error", str(exc))
        log.warning("sync_account_pool_transient", account_id=account_id, error=str(exc))
        return {"account_id": account_id, "folders": 0, "added": 0, "bodies": 0, "error": str(exc)}

    try:
        async with pool.connection() as conn:
            await imap_sync.sync_folders(session, account, conn)
    except MailAuthError as exc:
        await mark_account_status(session, account_uuid, "auth_error", str(exc))
        return {"account_id": account_id, "folders": 0, "added": 0, "bodies": 0, "error": str(exc)}
    except MailTransientError as exc:
        await mark_account_status(session, account_uuid, "error", str(exc))
        log.warning("sync_account_folders_transient", account_id=account_id, error=str(exc))
        return {"account_id": account_id, "folders": 0, "added": 0, "bodies": 0, "error": str(exc)}

    folder_ids = list(
        (
            await session.execute(select(Folder.id).where(Folder.account_id == account_uuid))
        ).scalars()
    )

    folders_synced = 0
    added_total = 0
    bodies_total = 0
    auth_error: str | None = None
    transient_error: str | None = None

    for folder_id in folder_ids:
        folder = await session.get(Folder, folder_id)
        fresh_account = await session.get(Account, account_uuid)
        if folder is None or fresh_account is None:
            continue
        folder_tag = str(folder_id)
        try:
            async with pool.connection() as conn:
                stats = await imap_sync.sync_folder(session, fresh_account, folder, conn)
            added_total += stats.added
            folders_synced += 1
        except MailAuthError as exc:
            auth_error = str(exc)
            break
        except MailTransientError as exc:
            transient_error = str(exc)
            log.warning(
                "sync_account_folder_transient",
                account_id=account_id,
                folder_id=folder_tag,
                error=str(exc),
            )
            continue
        except Exception as exc:
            transient_error = f"{type(exc).__name__}: {exc}"
            log.exception("sync_account_folder_error", account_id=account_id, folder_id=folder_tag)
            continue

        folder = await session.get(Folder, folder_id)
        fresh_account = await session.get(Account, account_uuid)
        if folder is None or fresh_account is None:
            continue
        try:
            async with pool.connection() as conn:
                bodies_total += await imap_sync.fetch_bodies(
                    session, fresh_account, folder, conn, limit=settings.body_backfill_max_per_run
                )
        except MailAuthError as exc:
            auth_error = str(exc)
            break
        except MailTransientError as exc:
            transient_error = str(exc)
            log.warning(
                "sync_account_bodies_transient",
                account_id=account_id,
                folder_id=folder_tag,
                error=str(exc),
            )
            continue
        except Exception as exc:
            transient_error = f"{type(exc).__name__}: {exc}"
            log.exception("sync_account_bodies_error", account_id=account_id, folder_id=folder_tag)
            continue

    if auth_error is not None:
        await mark_account_status(session, account_uuid, "auth_error", auth_error)
        return {
            "account_id": account_id,
            "folders": folders_synced,
            "added": added_total,
            "bodies": bodies_total,
            "error": auth_error,
        }

    if folder_ids and folders_synced == 0 and transient_error is not None:
        # Not a single folder succeeded: the server/account is having trouble. Marking
        # `active` here would hide the disruption from the GUI, so mark `error` without
        # updating `last_sync_at`.
        await mark_account_status(session, account_uuid, "error", transient_error)
        return {
            "account_id": account_id,
            "folders": 0,
            "added": added_total,
            "bodies": bodies_total,
            "error": transient_error,
        }

    await mark_account_status(session, account_uuid, "active")
    await stamp_last_sync(session, account_uuid)
    return {
        "account_id": account_id,
        "folders": folders_synced,
        "added": added_total,
        "bodies": bodies_total,
    }


# --- sync_account: Graph path -----------------------------------------------------------


async def _sync_graph_account(session: AsyncSession, account: Account) -> dict:
    account_uuid = account.id
    account_id = str(account_uuid)
    try:
        gs = await get_graph_settings(session, account_uuid)
    except AccountConfigError as exc:
        await mark_account_status(session, account_uuid, "auth_error", str(exc))
        return {"account_id": account_id, "folders": 0, "added": 0, "bodies": 0, "error": str(exc)}

    client = GraphClient.from_stored(account_uuid, gs.token_cache_enc, gs.home_account_id)
    folders_synced = 0
    added_total = 0
    bodies_total = 0
    auth_error: str | None = None
    transient_error: str | None = None

    try:
        folder_ids: list[uuid.UUID] = []
        try:
            folder_ids = [row.id for row in await graph_sync.sync_folders(session, account, client)]
        except GraphAuthError as exc:
            auth_error = str(exc)
        except GraphTransientError as exc:
            log.warning(
                "sync_account_graph_folders_transient", account_id=account_id, error=str(exc)
            )

        for folder_id in folder_ids if auth_error is None else []:
            # Same as the IMAP path: reload after a possible rollback.
            folder = await session.get(Folder, folder_id)
            fresh_account = await session.get(Account, account_uuid)
            if folder is None or fresh_account is None:
                continue
            folder_tag = str(folder_id)
            try:
                stats = await graph_sync.sync_folder(session, fresh_account, folder, client)
                added_total += stats.added
                folders_synced += 1
            except GraphAuthError as exc:
                auth_error = str(exc)
                break
            except GraphTransientError as exc:
                transient_error = str(exc)
                log.warning(
                    "sync_account_graph_folder_transient",
                    account_id=account_id,
                    folder_id=folder_tag,
                    error=str(exc),
                )
                continue
            except Exception as exc:
                transient_error = f"{type(exc).__name__}: {exc}"
                log.exception(
                    "sync_account_graph_folder_error",
                    account_id=account_id,
                    folder_id=folder_tag,
                )
                continue

            folder = await session.get(Folder, folder_id)
            fresh_account = await session.get(Account, account_uuid)
            if folder is None or fresh_account is None:
                continue
            try:
                bodies_total += await graph_sync.fetch_bodies(
                    session, fresh_account, folder, client
                )
            except GraphAuthError as exc:
                auth_error = str(exc)
                break
            except GraphTransientError as exc:
                transient_error = str(exc)
                log.warning(
                    "sync_account_graph_bodies_transient",
                    account_id=account_id,
                    folder_id=folder_tag,
                    error=str(exc),
                )
                continue
            except Exception as exc:
                transient_error = f"{type(exc).__name__}: {exc}"
                log.exception(
                    "sync_account_graph_bodies_error",
                    account_id=account_id,
                    folder_id=folder_tag,
                )
                continue
    finally:
        # The token cache may have expired due to rollback: refetch before storing.
        fresh_settings = await session.get(GraphSettings, account_uuid)
        cache_blob = dump_token_cache(account_uuid, client.token_cache)
        if cache_blob is not None and fresh_settings is not None:
            fresh_settings.token_cache_enc = cache_blob
            await session.commit()
        await client.aclose()

    if auth_error is not None:
        await mark_account_status(session, account_uuid, "auth_error", auth_error)
        return {
            "account_id": account_id,
            "folders": folders_synced,
            "added": added_total,
            "bodies": bodies_total,
            "error": auth_error,
        }

    if folder_ids and folders_synced == 0 and transient_error is not None:
        # Same as the IMAP path: no meaningful progress = mark `error`.
        await mark_account_status(session, account_uuid, "error", transient_error)
        return {
            "account_id": account_id,
            "folders": 0,
            "added": added_total,
            "bodies": bodies_total,
            "error": transient_error,
        }

    await mark_account_status(session, account_uuid, "active")
    await stamp_last_sync(session, account_uuid)
    return {
        "account_id": account_id,
        "folders": folders_synced,
        "added": added_total,
        "bodies": bodies_total,
    }


async def sync_account(ctx: dict, account_id: str) -> dict:
    """One full sync cycle (all folders + body backfill) for a single account.

    Two cycles for the same account must not overlap. The scheduled cron, the IDLE watcher and
    a manual `POST /accounts/{id}/sync` can all fire within the same second, and concurrent
    cycles write the same placement rows in different orders, which Postgres resolves by
    killing one of them with a deadlock. The Redis flag makes the second caller step aside:
    the cycle already running covers the same mailbox state anyway.
    """
    redis = ctx.get("redis")
    lock_key = f"{SYNC_LOCK_PREFIX}{account_id}"
    if redis is not None:
        # This job is no longer waiting, so the cron may queue the next one.
        with contextlib.suppress(Exception):
            await redis.delete(f"{SYNC_QUEUED_PREFIX}{account_id}")
        if not await redis.set(lock_key, "1", ex=SYNC_LOCK_TTL_S, nx=True):
            log.info("sync_account_already_running", account_id=account_id)
            return {
                "account_id": account_id,
                "skipped": True,
                "folders": 0,
                "added": 0,
                "bodies": 0,
            }

    try:
        async with SessionLocal() as session:
            account = await session.get(Account, uuid.UUID(account_id))
            if account is None or account.status in ("disabled", "auth_error"):
                return {
                    "account_id": account_id,
                    "skipped": True,
                    "folders": 0,
                    "added": 0,
                    "bodies": 0,
                }

            if account.provider == "imap":
                return await _sync_imap_account(session, account)
            if account.provider == "graph":
                return await _sync_graph_account(session, account)

            log.warning(
                "sync_account_unknown_provider", account_id=account_id, provider=account.provider
            )
            return {
                "account_id": account_id,
                "skipped": True,
                "folders": 0,
                "added": 0,
                "bodies": 0,
            }
    finally:
        if redis is not None:
            with contextlib.suppress(Exception):
                await redis.delete(lock_key)


async def deliver_outbox(ctx: dict, account_id: str) -> int:
    """Send the oldest `queued` `outbox` row for one account (called from the API)."""
    async with SessionLocal() as session:
        account = await session.get(Account, uuid.UUID(account_id))
        if account is None:
            return 0
        return await outbox.send_pending(session, account, limit=10)


async def backfill_bodies(
    ctx: dict, account_id: str, folder_id: str | None = None, limit: int | None = None
) -> int:
    """Body backfill only: one folder (`folder_id`) or all of an account's folders."""
    async with SessionLocal() as session:
        account = await session.get(Account, uuid.UUID(account_id))
        if account is None:
            return 0

        folders: Sequence[Folder]
        if folder_id is not None:
            folder = await session.get(Folder, uuid.UUID(folder_id))
            folders = [folder] if folder is not None and folder.account_id == account.id else []
        else:
            folders = (
                (await session.execute(select(Folder).where(Folder.account_id == account.id)))
                .scalars()
                .all()
            )

        stored_total = 0

        if account.provider == "imap":
            try:
                pool = await get_account_pool(session, account, session_factory=SessionLocal)
            except (MailAuthError, AccountConfigError) as exc:
                log.warning("backfill_bodies_pool_error", account_id=account_id, error=str(exc))
                return 0
            for folder in folders:
                try:
                    async with pool.connection() as conn:
                        stored_total += await imap_sync.fetch_bodies(
                            session, account, folder, conn, limit=limit
                        )
                except (MailTransientError, MailAuthError) as exc:
                    log.warning(
                        "backfill_bodies_folder_error",
                        account_id=account_id,
                        folder_id=str(folder.id),
                        error=str(exc),
                    )
                    continue

        elif account.provider == "graph":
            try:
                gs = await get_graph_settings(session, account.id)
            except AccountConfigError as exc:
                log.warning(
                    "backfill_bodies_graph_settings_error", account_id=account_id, error=str(exc)
                )
                return 0
            client = GraphClient.from_stored(account.id, gs.token_cache_enc, gs.home_account_id)
            try:
                for folder in folders:
                    try:
                        stored_total += await graph_sync.fetch_bodies(
                            session, account, folder, client, limit=limit
                        )
                    except (GraphTransientError, GraphAuthError) as exc:
                        log.warning(
                            "backfill_bodies_graph_folder_error",
                            account_id=account_id,
                            folder_id=str(folder.id),
                            error=str(exc),
                        )
                        continue
            finally:
                cache_blob = dump_token_cache(account.id, client.token_cache)
                if cache_blob is not None:
                    gs.token_cache_enc = cache_blob
                    await session.commit()
                await client.aclose()

        return stored_total


async def janitor_sweep(ctx: dict) -> dict:
    """Re-check quarantined accounts and delete dead agent tokens.

    Runs on its own cron rather than inside `sync_account`: the sweep talks to providers for
    accounts that sync deliberately skips, and one slow login must not delay a mail cycle.
    """
    async with SessionLocal() as session:
        report = await maintenance.run_sweep(session)
    log.info("janitor_sweep", **report.as_dict())
    return report.as_dict()


async def sync_all_accounts(ctx: dict) -> int:
    """Enqueue `sync_account` for accounts that are due (`last_sync_at` expired).

    At most one pending job per account. This cron fires every 30 seconds, so without the
    marker a worker that falls behind — a slow mailbox, or every job slot taken by IDLE
    watchers — accumulates thousands of redundant sync jobs and starves anything a user
    triggers by hand.
    """
    redis = ctx["redis"]
    async with SessionLocal() as session:
        due_ids = (
            (
                await session.execute(
                    select(Account.id)
                    .where(Account.status.in_(("active", "error")))
                    .where(
                        or_(
                            Account.last_sync_at.is_(None),
                            text("last_sync_at < now() - make_interval(secs => sync_interval_s)"),
                        )
                    )
                )
            )
            .scalars()
            .all()
        )

    enqueued = 0
    for account_id in due_ids:
        pending_key = f"{SYNC_QUEUED_PREFIX}{account_id}"
        if not await redis.set(pending_key, "1", ex=SYNC_QUEUED_TTL_S, nx=True):
            continue
        job = await redis.enqueue_job("sync_account", str(account_id))
        if job is None:
            await redis.delete(pending_key)
            continue
        enqueued += 1
    return enqueued


async def ensure_idle_watchers(ctx: dict) -> int:
    """Enqueue `idle_watch` for active IMAP accounts that don't have a live watcher yet."""
    redis = ctx["redis"]
    async with SessionLocal() as session:
        account_ids = (
            (
                await session.execute(
                    select(Account.id).where(Account.provider == "imap", Account.status == "active")
                )
            )
            .scalars()
            .all()
        )

    enqueued = 0
    for account_id in account_ids:
        lock_key = _idle_lock_key(str(account_id))
        if await redis.exists(lock_key):
            continue
        job = await redis.enqueue_job("idle_watch", str(account_id))
        if job is not None:
            enqueued += 1
    return enqueued


async def _select_idle_folder(session: AsyncSession, account: Account) -> Folder | None:
    folder = (
        await session.execute(
            select(Folder).where(Folder.account_id == account.id, Folder.role == "inbox").limit(1)
        )
    ).scalar_one_or_none()
    if folder is not None:
        return folder
    return (
        await session.execute(
            select(Folder)
            .where(Folder.account_id == account.id, Folder.is_pinned.is_(True))
            .limit(1)
        )
    ).scalar_one_or_none()


async def _idle_backoff(account_id: str, attempt: int, exc: Exception) -> bool:
    """Wait before reconnecting an IDLE watcher; `False` once the attempts are spent.

    A watcher that keeps failing holds one of the worker's `max_jobs` slots for its whole
    hour-long timeout, so a handful of unreachable accounts can starve every sync job. After
    the last backoff the watcher gives up and returns; `ensure_idle_watchers` starts a fresh
    one at its next five-minute tick, and the slot is free until then.
    """
    if attempt >= len(IDLE_RECONNECT_BACKOFFS_S):
        log.warning("idle_watch_giving_up", account_id=account_id, error=str(exc))
        return False
    delay = IDLE_RECONNECT_BACKOFFS_S[attempt]
    log.warning("idle_watch_reconnect", account_id=account_id, error=str(exc), delay=delay)
    await asyncio.sleep(delay)
    return True


async def idle_watch(ctx: dict, account_id: str) -> None:
    """Watch one account's inbox folder over IMAP IDLE; sync inline when the server reports
    a change.

    Long-lived job — see `main.py` for the custom timeout. Respects job cancellation:
    `asyncio.CancelledError` is left to propagate after the connection is closed and the
    Redis key is deleted.
    """
    redis = ctx.get("redis")
    lock_key = _idle_lock_key(account_id)
    # Held for the whole job, reconnect attempts included: while a watcher is retrying it is
    # still the owner of this account and must not be doubled up by `ensure_idle_watchers`.
    heartbeat = asyncio.create_task(_hold_idle_lock(redis, lock_key)) if redis else None
    attempt = 0
    try:
        while True:
            async with SessionLocal() as session:
                account = await session.get(Account, uuid.UUID(account_id))
                if (
                    account is None
                    or account.provider != "imap"
                    or account.status in ("disabled", "auth_error")
                ):
                    return
                folder = await _select_idle_folder(session, account)
                if folder is None:
                    log.info("idle_watch_no_folder", account_id=account_id)
                    return
                try:
                    pool: ImapPool = await get_account_pool(
                        session, account, session_factory=SessionLocal
                    )
                except (MailAuthError, AccountConfigError) as exc:
                    await mark_account_status(session, account.id, "auth_error", str(exc))
                    return
                folder_path = folder.path or folder.name

            conn = None
            try:
                conn = await pool.open_idle_connection()
                await conn.select_folder(folder_path, readonly=True)
                await conn.idle()
                attempt = 0
                while True:
                    response = await conn.idle_check(settings.idle_renew_s)
                    await conn.idle_done()
                    if _idle_has_changes(response):
                        try:
                            await sync_account(ctx, account_id)
                        except Exception:
                            log.exception("idle_watch_sync_failed", account_id=account_id)
                    await conn.idle()
            except asyncio.CancelledError:
                raise
            except MailAuthError as exc:
                async with SessionLocal() as session:
                    await mark_account_status(
                        session, uuid.UUID(account_id), "auth_error", str(exc)
                    )
                return
            except MailTransientError as exc:
                if _IDLE_HANDSHAKE_RACE in str(exc):
                    # Not a failure: the server pushed an untagged EXISTS/FETCH instead of the
                    # IDLE continuation. Sync what arrived and reconnect straight away —
                    # backing off here would delay every busy mailbox by seconds.
                    log.info("idle_watch_change_during_handshake", account_id=account_id)
                    try:
                        await sync_account(ctx, account_id)
                    except Exception:
                        log.exception("idle_watch_sync_failed", account_id=account_id)
                    attempt = 0
                    continue
                if not await _idle_backoff(account_id, attempt, exc):
                    return
                attempt += 1
                continue
            except OSError as exc:
                if not await _idle_backoff(account_id, attempt, exc):
                    return
                attempt += 1
                continue
            finally:
                if conn is not None:
                    await conn.close()
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        if redis is not None:
            with contextlib.suppress(Exception):
                await redis.delete(lock_key)
