# Lenovmail — authored by satuapps
"""Pooled IMAP connections must be checked before they are handed out again.

Mail servers close idle connections without warning. The pool used to return them as-is, so
the first command of the next sync cycle failed with `socket error: EOF` and the account was
reported as an outage even though the server was fine.
"""

from __future__ import annotations

from lenovmail.providers.imap_pool import AccountImapConfig, ImapPool, MailTransientError

CONFIG = AccountImapConfig(
    host="imap.example.com", port=993, security="ssl", username="u", password="p"
)


class FakeConn:
    def __init__(self, *, alive: bool) -> None:
        self.alive = alive
        self.closed = False
        self.noops = 0

    async def noop(self) -> str:
        self.noops += 1
        if not self.alive:
            raise MailTransientError("noop: socket error: EOF")
        return "OK"

    async def close(self) -> None:
        self.closed = True


def pool_with(idle: list[FakeConn], fresh: FakeConn) -> ImapPool:
    pool = ImapPool(account_id="a", config=CONFIG)
    pool._idle = list(idle)  # type: ignore[assignment]

    async def new_connection() -> FakeConn:
        return fresh

    pool._new_connection = new_connection  # type: ignore[assignment,method-assign]
    return pool


async def test_live_pooled_connection_is_reused():
    live = FakeConn(alive=True)
    fresh = FakeConn(alive=True)
    pool = pool_with([live], fresh)

    conn = await pool._acquire()

    assert conn is live
    assert live.noops == 1 and live.closed is False


async def test_dead_pooled_connection_is_dropped_and_replaced():
    dead = FakeConn(alive=False)
    fresh = FakeConn(alive=True)
    pool = pool_with([dead], fresh)

    conn = await pool._acquire()

    assert conn is fresh, "a closed connection must not be handed to the sync layer"
    assert dead.closed is True


async def test_every_dead_connection_is_drained_before_opening_a_new_one():
    dead = [FakeConn(alive=False), FakeConn(alive=False)]
    fresh = FakeConn(alive=True)
    pool = pool_with(dead, fresh)

    conn = await pool._acquire()

    assert conn is fresh
    assert all(c.closed for c in dead)
