# Lenovmail — authored by satuapps (satuapps.com)
"""Worker concurrency regressions, all three found by running the stack against a real server.

* GreenMail answers `IDLE` with an untagged `* 43 EXISTS` before the continuation line.
  imapclient raises on that, and the watcher used to treat it as a dead connection: five
  seconds of backoff, an error-level log, and a reconnect for every message that arrived.
* Because each reconnect ran `sync_account` inline while the cron cycle was also running, two
  cycles wrote the same placement rows in opposite order and Postgres killed one of them with
  `deadlock detected`.
* The watcher's liveness key expired 90 seconds into a 25-minute `idle_check`, so
  `ensure_idle_watchers` kept starting duplicates until every worker slot was taken by an
  IDLE watcher and 1847 sync jobs were left queued behind them.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import Counter

import pytest

from lenovmail.providers.imap_pool import MailTransientError
from lenovmail.workers import tasks


class FakeRedis:
    """The handful of calls the worker makes: `set(..., nx=True)`, `delete`, `enqueue_job`."""

    def __init__(self) -> None:
        self.keys: dict[str, str] = {}
        self.deleted: list[str] = []
        self.enqueued: list[str] = []
        self.sets: Counter[str] = Counter()

    async def set(self, key: str, value: str, ex: float | None = None, nx: bool = False) -> bool:
        if nx and key in self.keys:
            return False
        self.keys[key] = value
        self.sets[key] += 1
        return True

    async def delete(self, key: str) -> int:
        self.deleted.append(key)
        return int(self.keys.pop(key, None) is not None)

    async def enqueue_job(self, function: str, *args: str) -> object:
        self.enqueued.append(args[0])
        return object()


async def test_second_sync_cycle_steps_aside_while_the_first_runs(account, monkeypatch):
    started = 0

    async def slow_sync(session, acct) -> dict:
        nonlocal started
        started += 1
        await asyncio.sleep(0.05)
        return {"account_id": str(acct.id), "folders": 1, "added": 0, "bodies": 0}

    monkeypatch.setattr(tasks, "_sync_imap_account", slow_sync)
    ctx = {"redis": FakeRedis()}

    first, second = await asyncio.gather(
        tasks.sync_account(ctx, str(account.id)),
        tasks.sync_account(ctx, str(account.id)),
    )

    outcomes = sorted([first.get("skipped", False), second.get("skipped", False)])
    assert outcomes == [False, True], "exactly one cycle should run"
    assert started == 1


async def test_lock_is_released_even_when_the_cycle_raises(account, monkeypatch):
    async def exploding_sync(session, acct) -> dict:
        raise RuntimeError("sync blew up")

    monkeypatch.setattr(tasks, "_sync_imap_account", exploding_sync)
    redis = FakeRedis()

    with pytest.raises(RuntimeError):
        await tasks.sync_account({"redis": redis}, str(account.id))

    assert f"{tasks.SYNC_LOCK_PREFIX}{account.id}" in redis.deleted
    assert f"{tasks.SYNC_LOCK_PREFIX}{account.id}" not in redis.keys


async def test_due_account_is_queued_once_until_its_job_runs(session, account):
    """The 30-second cron must not pile up a second job while the first is still waiting."""
    redis = FakeRedis()
    account_id = str(account.id)

    await tasks.sync_all_accounts({"redis": redis})
    await tasks.sync_all_accounts({"redis": redis})
    assert redis.enqueued.count(account_id) == 1

    # The job starts: it clears its pending marker, so the next cron tick may queue again.
    await redis.delete(f"{tasks.SYNC_QUEUED_PREFIX}{account_id}")
    await tasks.sync_all_accounts({"redis": redis})
    assert redis.enqueued.count(account_id) == 2


class RacingConn:
    """IDLE connection that reports a change during the handshake, then stops the watcher."""

    def __init__(self) -> None:
        self.idle_calls = 0

    async def select_folder(self, folder: str, readonly: bool = False) -> dict:
        return {}

    async def idle(self) -> None:
        self.idle_calls += 1
        if self.idle_calls == 1:
            raise MailTransientError("idle: Unexpected IDLE response: b'* 43 EXISTS'")
        raise asyncio.CancelledError

    async def close(self) -> None:
        return None


async def test_idle_handshake_race_syncs_instead_of_backing_off(
    session, account, folder, monkeypatch
):
    conn = RacingConn()
    synced: list[str] = []
    slept: list[int] = []

    class FakePool:
        async def open_idle_connection(self) -> RacingConn:
            return conn

    async def fake_pool(*args, **kwargs) -> FakePool:
        return FakePool()

    async def fake_sync(ctx, account_id: str) -> dict:
        synced.append(account_id)
        return {"account_id": account_id, "folders": 0, "added": 0, "bodies": 0}

    async def fake_backoff(account_id: str, attempt: int, exc: Exception) -> None:
        slept.append(attempt)

    monkeypatch.setattr(tasks, "get_account_pool", fake_pool)
    monkeypatch.setattr(tasks, "sync_account", fake_sync)
    monkeypatch.setattr(tasks, "_idle_backoff", fake_backoff)

    with pytest.raises(asyncio.CancelledError):
        await tasks.idle_watch({}, str(account.id))

    assert synced == [str(account.id)], "the announced change must be synced"
    assert slept == [], "a handshake race must not spend a reconnect backoff"
    assert conn.idle_calls == 2, "the watcher must retry IDLE immediately"


async def test_watcher_keeps_its_liveness_key_alive_while_idle_blocks(monkeypatch):
    """`idle_check` blocks for minutes; the key must not lapse and invite a second watcher."""
    monkeypatch.setattr(tasks, "IDLE_WATCH_LOCK_TTL_S", 0.3)
    redis = FakeRedis()
    key = tasks._idle_lock_key("acct")

    heartbeat = asyncio.create_task(tasks._hold_idle_lock(redis, key))
    await asyncio.sleep(0.35)
    heartbeat.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await heartbeat

    assert redis.sets[key] >= 3, "the key must be re-set several times per TTL"


class DeadConn:
    """IDLE connection whose server never answers — the unreachable-account case."""

    def __init__(self) -> None:
        self.attempts = 0

    async def select_folder(self, folder: str, readonly: bool = False) -> dict:
        return {}

    async def idle(self) -> None:
        self.attempts += 1
        raise OSError("[Errno 111] Connection refused")

    async def close(self) -> None:
        return None


async def test_watcher_gives_up_instead_of_holding_a_slot_forever(
    session, account, folder, monkeypatch
):
    """An account that never answers must not pin one of the worker's job slots for an hour."""
    conn = DeadConn()

    class FakePool:
        async def open_idle_connection(self) -> DeadConn:
            return conn

    async def fake_pool(*args, **kwargs) -> FakePool:
        return FakePool()

    monkeypatch.setattr(tasks, "get_account_pool", fake_pool)
    monkeypatch.setattr(tasks, "IDLE_RECONNECT_BACKOFFS_S", (0, 0, 0))

    await tasks.idle_watch({}, str(account.id))

    assert conn.attempts == len(tasks.IDLE_RECONNECT_BACKOFFS_S) + 1, (
        "the watcher must retry once per backoff step, then return"
    )
