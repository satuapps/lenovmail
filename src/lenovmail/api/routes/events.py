# Lenovmail — authored by satuapps (satuapps.com)
"""Server-Sent Events: the GUI receives sync updates without polling.

One connection per tab subscribes to the user's Redis channel (`events:user:<id>`), so
messages never leak between users. A heartbeat is sent periodically so proxies don't
close an idle connection.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ...logging import get_logger
from ...sync.events import channel_for
from ..deps import PrincipalDep, RedisDep

log = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["events"])

HEARTBEAT_S = 20.0
IDLE_TIMEOUT_S = 300.0


@router.get("/events")
async def stream_events(
    request: Request, principal: PrincipalDep, redis: RedisDep
) -> StreamingResponse:
    """Event stream for the currently logged-in user (`text/event-stream` format)."""

    async def generator() -> AsyncIterator[bytes]:
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel_for(principal.user_id))
        try:
            yield b": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=HEARTBEAT_S
                )
                if message is None:
                    yield b": ping\n\n"
                    continue
                data = message.get("data")
                if isinstance(data, bytes):
                    data = data.decode("utf-8", "replace")
                yield f"data: {data}\n\n".encode()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("sse_stream_failed", user_id=str(principal.user_id), exc_info=True)
        finally:
            await pubsub.unsubscribe(channel_for(principal.user_id))
            await pubsub.aclose()

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
