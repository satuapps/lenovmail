# Lenovmail — authored by satuapps
"""Event publishing to the GUI over Redis pub/sub.

`init_events()` is called at API/worker/idler startup. When not yet initialized
(e.g. during unit tests), `publish()` becomes a no-op so the sync layer doesn't
depend on Redis to be testable.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import redis.asyncio as redis_async

from ..logging import get_logger

log = get_logger(__name__)

EVENT_TYPES = (
    "message.new",
    "message.updated",
    "folder.counts",
    "account.status",
    "sync.progress",
)

_client: redis_async.Redis | None = None


def channel_for(user_id: uuid.UUID | str) -> str:
    return f"events:user:{user_id}"


def init_events(redis_url: str) -> redis_async.Redis:
    global _client
    _client = redis_async.from_url(redis_url, decode_responses=True)
    return _client


def get_client() -> redis_async.Redis | None:
    return _client


async def close_events() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def publish(user_id: uuid.UUID | str, event_type: str, payload: dict[str, Any]) -> None:
    """Send one event to the user's channel. No-op if Redis hasn't been initialized."""
    if _client is None:
        return
    if event_type not in EVENT_TYPES:
        log.warning("unknown_event_type", event_type=event_type)
    message = json.dumps({"type": event_type, "payload": payload}, default=str)
    try:
        await _client.publish(channel_for(user_id), message)
    except Exception:
        # A notification failure must never fail the sync.
        log.warning("event_publish_failed", event_type=event_type, exc_info=True)
