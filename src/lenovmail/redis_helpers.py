# Lenovmail — authored by satuapps (satuapps.com)
"""Thin wrapper around redis-py.

`redis.asyncio.Redis` inherits commands from a dual-mode class, so some methods are
typed `Union[Awaitable[T], T]` even though on an async client they are always
awaitable. This helper normalizes both shapes so callers can write a clear `await`
without `type: ignore` scattered everywhere.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable
from typing import cast


async def resolved[T](value: Awaitable[T] | T) -> T:
    if inspect.isawaitable(value):
        return await cast("Awaitable[T]", value)
    return value
