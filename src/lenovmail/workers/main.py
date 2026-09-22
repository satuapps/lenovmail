# Lenovmail — authored by satuapps (satuapps.com)
"""arq worker entry point: job registration, cron, and shared pool lifecycle.

Run with: `uv run arq lenovmail.workers.main.WorkerSettings`
"""

from __future__ import annotations

from arq import cron, func
from arq.connections import RedisSettings

from ..config import settings
from ..db import dispose_engine
from ..logging import configure_logging, get_logger
from ..providers.imap_pool import close_all_pools
from ..sync.events import close_events, init_events
from .tasks import (
    backfill_bodies,
    deliver_outbox,
    ensure_idle_watchers,
    idle_watch,
    janitor_sweep,
    mine_values_backfill,
    sync_account,
    sync_all_accounts,
)

log = get_logger(__name__)

# `idle_watch` holds one job for many rounds of `idle_renew_s` (default 1500 seconds) —
# much longer than the global `worker_job_timeout_s`. It is wrapped in `func()` with its
# own timeout so arq does not cancel it mid-IDLE-cycle.
IDLE_WATCH_TIMEOUT_S = 3600


async def on_startup(ctx: dict) -> None:
    configure_logging(settings.log_level)
    init_events(settings.redis_url)
    log.info("worker_startup")


async def on_shutdown(ctx: dict) -> None:
    await close_all_pools()
    await close_events()
    await dispose_engine()
    log.info("worker_shutdown")


class WorkerSettings:
    functions = [
        sync_account,
        backfill_bodies,
        sync_all_accounts,
        func(idle_watch, timeout=IDLE_WATCH_TIMEOUT_S),
        ensure_idle_watchers,
        deliver_outbox,
        janitor_sweep,
        mine_values_backfill,
    ]
    cron_jobs = [
        cron(sync_all_accounts, second={0, 30}, run_at_startup=True),
        cron(ensure_idle_watchers, minute=set(range(0, 60, 5))),
        # Hourly, off the top of the hour so it never queues behind the sync fan-out.
        cron(janitor_sweep, minute={17}),
        # Every ten minutes: only messages ingested before the extractor existed qualify,
        # so this drains to nothing and then costs one indexed lookup per run.
        cron(mine_values_backfill, minute=set(range(0, 60, 10))),
    ]
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = settings.worker_max_jobs
    job_timeout = settings.worker_job_timeout_s
    keep_result = 3600
    on_startup = on_startup
    on_shutdown = on_shutdown
