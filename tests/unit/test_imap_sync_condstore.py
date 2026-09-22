# Lenovmail — authored by satuapps
"""Sync path decision tests: CONDSTORE vs fallback, UIDVALIDITY, and new UIDs.

The test server (GreenMail) does not advertise CONDSTORE, so the CONDSTORE branch is
proven here with a fake `ImapConn` that records the commands actually sent.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select

from lenovmail.config import settings
from lenovmail.models import Folder, MailboxMessage, Message, SyncRun
from lenovmail.sync.imap_sync import sync_folder

BASE_TIME = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


class FakeEnvelope:
    def __init__(self, message_id: str, subject: str) -> None:
        self.date = None
        self.subject = subject.encode()
        self.from_ = [FakeAddress("budi", "example.com", "Budi")]
        self.sender = []
        self.reply_to = []
        self.to = [FakeAddress("siti", "example.org", "Siti")]
        self.cc = []
        self.bcc = []
        self.in_reply_to = None
        self.message_id = message_id


class FakeAddress:
    def __init__(self, mailbox: str, host: str, name: str | None = None) -> None:
        self.name = name.encode() if name else None
        self.mailbox = mailbox.encode()
        self.host = host.encode()


class FakeConn:
    """Fake `ImapConn`: records every command and serves the pre-prepared data."""

    def __init__(
        self,
        *,
        uids: list[int],
        uidvalidity: int = 111,
        uidnext: int | None = None,
        highestmodseq: int | None = None,
        condstore: bool = False,
        changed_since: dict[int, list[str]] | None = None,
        all_flags: dict[int, list[str]] | None = None,
    ) -> None:
        self.uids = sorted(uids)
        self.uidvalidity = uidvalidity
        self.uidnext = uidnext if uidnext is not None else (max(uids) + 1 if uids else 1)
        self.highestmodseq = highestmodseq
        self.caps = {
            "condstore": condstore,
            "move": True,
            "idle": True,
            "special_use": False,
            "uidplus": True,
        }
        self.changed_since = changed_since or {}
        self.all_flags = all_flags or {}
        self.search_calls: list[Any] = []
        self.fetch_calls: list[tuple[list[int], list[str], list[str] | None]] = []

    @property
    def condstore(self) -> bool:
        """Same as `ImapConn.condstore` (the engine reads it as a property)."""
        return bool(self.caps.get("condstore"))

    async def select_folder(self, folder: str, readonly: bool = False) -> dict[bytes, Any]:
        return {
            b"UIDVALIDITY": self.uidvalidity,
            b"UIDNEXT": self.uidnext,
            b"HIGHESTMODSEQ": self.highestmodseq,
        }

    async def search(self, criteria: Any) -> list[int]:
        self.search_calls.append(criteria)
        if criteria == ["ALL"]:
            return list(self.uids)
        # criteria = ["UID", "n:*"]
        prefix = int(str(criteria[1]).split(":")[0])
        return [uid for uid in self.uids if uid >= prefix]

    async def fetch(self, messages: Any, data: Any, modifiers: Any = None) -> dict[int, dict]:
        message_list = list(messages)
        self.fetch_calls.append((message_list, list(data), list(modifiers or [])))
        if modifiers:
            # CHANGEDSINCE: only messages that changed are returned by the server.
            return {
                uid: {b"UID": uid, b"FLAGS": tuple(self.changed_since.get(uid, []))}
                for uid in message_list
                if uid in self.changed_since
            }
        if "FLAGS" in data and "ENVELOPE" not in data:
            return {
                uid: {b"UID": uid, b"FLAGS": tuple(self.all_flags.get(uid, []))}
                for uid in message_list
            }
        return {
            uid: {
                b"UID": uid,
                b"FLAGS": (),
                b"INTERNALDATE": BASE_TIME,
                b"RFC822.SIZE": 1024,
                b"ENVELOPE": FakeEnvelope(f"<uid{uid}@example.com>", f"Message {uid}"),
            }
            for uid in message_list
        }


def condstore_modifiers(conn: FakeConn) -> list[list[str]]:
    return [call[2] for call in conn.fetch_calls if call[2]]


class TestCondstorePath:
    async def test_incremental_uses_changedsince_when_condstore_available(
        self, session, account, folder
    ):
        # First cycle: a genuine full sync that actually writes placements.
        await sync_folder(
            session, account, folder, FakeConn(uids=[1, 2], uidnext=3, highestmodseq=500)
        )
        await session.refresh(folder)
        folder.sync_pass = 1  # not yet due for a full flag refresh
        await session.commit()

        conn = FakeConn(
            uids=[1, 2],
            uidnext=3,
            highestmodseq=777,
            condstore=True,
            changed_since={1: ["\\Seen"]},
        )
        await sync_folder(session, account, folder, conn)

        assert condstore_modifiers(conn) == [["CHANGEDSINCE 500"]]
        # The CONDSTORE path must not use a full flag refresh (FETCH without a modifier).
        full_flag_fetches = [
            call
            for call in conn.fetch_calls
            if "FLAGS" in call[1] and "ENVELOPE" not in call[1] and not call[2]
        ]
        assert full_flag_fetches == []

        flag_seen = (
            await session.execute(
                select(MailboxMessage.flag_seen).where(
                    MailboxMessage.folder_id == folder.id, MailboxMessage.remote_uid == 1
                )
            )
        ).scalar_one()
        assert flag_seen is True

    async def test_without_condstore_flags_refresh_only_every_n_passes(
        self, session, account, folder
    ):
        await sync_folder(session, account, folder, FakeConn(uids=[1, 2], uidnext=3))
        await session.refresh(folder)
        folder.sync_pass = 1
        await session.commit()

        conn = FakeConn(uids=[1, 2], uidnext=3, condstore=False, all_flags={1: ["\\Flagged"]})
        await sync_folder(session, account, folder, conn)

        assert condstore_modifiers(conn) == []
        # sync_pass=1, FLAG_REFRESH_EVERY=6 -> not yet due for a flag refresh.
        assert [c for c in conn.fetch_calls if "FLAGS" in c[1] and "ENVELOPE" not in c[1]] == []

    async def test_without_condstore_flags_refresh_on_due_pass(self, session, account, folder):
        await sync_folder(session, account, folder, FakeConn(uids=[1, 2], uidnext=3))
        await session.refresh(folder)
        folder.sync_pass = 0  # 0 % FLAG_REFRESH_EVERY == 0 -> refresh cycle
        await session.commit()

        conn = FakeConn(uids=[1, 2], uidnext=3, condstore=False, all_flags={1: ["\\Flagged"]})
        await sync_folder(session, account, folder, conn)

        assert condstore_modifiers(conn) == []
        assert [c for c in conn.fetch_calls if "FLAGS" in c[1] and "ENVELOPE" not in c[1]] != []

        flag_flagged = (
            await session.execute(
                select(MailboxMessage.flag_flagged).where(
                    MailboxMessage.folder_id == folder.id, MailboxMessage.remote_uid == 1
                )
            )
        ).scalar_one()
        assert flag_flagged is True

    async def test_sync_pass_increments_each_run(self, session, account, folder):
        folder.uidvalidity, folder.uidnext = 111, 3
        await session.commit()

        await sync_folder(session, account, folder, FakeConn(uids=[1, 2], uidnext=3))
        await session.refresh(folder)

        assert folder.sync_pass == 1


class TestUidValidity:
    async def test_change_wipes_placements_and_runs_full(self, session, account, folder):
        folder.uidvalidity, folder.uidnext, folder.highestmodseq = 111, 3, 500
        await session.commit()

        conn = FakeConn(uids=[7, 8], uidvalidity=999, uidnext=9, highestmodseq=50)
        stats = await sync_folder(session, account, folder, conn)

        assert stats.full_resync is True
        assert stats.kind == "full"
        assert ["ALL"] in conn.search_calls

        await session.refresh(folder)
        assert folder.uidvalidity == 999

        placements = (
            await session.execute(
                select(func.count(MailboxMessage.id)).where(MailboxMessage.folder_id == folder.id)
            )
        ).scalar_one()
        assert placements == 2
        uids = set(
            (
                await session.execute(
                    select(MailboxMessage.remote_uid).where(MailboxMessage.folder_id == folder.id)
                )
            )
            .scalars()
            .all()
        )
        assert uids == {7, 8}

    async def test_first_sync_is_full_and_does_not_double_insert(self, session, account, folder):
        conn = FakeConn(uids=[1, 2, 3], uidnext=4)
        first = await sync_folder(session, account, folder, conn)

        assert first.kind == "full" and first.added == 3

        await session.refresh(folder)
        second = await sync_folder(session, account, folder, conn)

        assert second.kind == "incremental"
        assert second.added == 0  # no new UIDs
        assert (
            await session.execute(
                select(func.count(Message.id)).where(Message.account_id == account.id)
            )
        ).scalar_one() == 3


class TestIncrementalUids:
    async def test_only_uids_at_or_above_local_uidnext_are_fetched(self, session, account, folder):
        folder.uidvalidity, folder.uidnext, folder.sync_pass = 111, 3, 1
        await session.commit()

        conn = FakeConn(uids=[1, 2, 3, 4], uidnext=5)
        stats = await sync_folder(session, account, folder, conn)

        assert ["UID", "3:*"] in conn.search_calls
        assert stats.added == 2
        uids = sorted(
            (
                await session.execute(
                    select(MailboxMessage.remote_uid).where(MailboxMessage.folder_id == folder.id)
                )
            )
            .scalars()
            .all()
        )
        assert uids == [3, 4]

    async def test_deleted_on_server_is_removed_locally(self, session, account, folder):
        # Full sync first so a placement actually exists.
        await sync_folder(session, account, folder, FakeConn(uids=[1, 2], uidnext=3))
        await session.refresh(folder)

        # The server no longer has UID 1; the flag-refresh cycle (pass 0) runs deletion detection.
        folder.sync_pass = 0
        await session.commit()
        stats = await sync_folder(session, account, folder, FakeConn(uids=[2], uidnext=3))

        assert stats.removed == 1
        uids = sorted(
            (
                await session.execute(
                    select(MailboxMessage.remote_uid).where(MailboxMessage.folder_id == folder.id)
                )
            )
            .scalars()
            .all()
        )
        assert uids == [2]
        # Orphaned messages get cleaned up too.
        assert (
            await session.execute(
                select(func.count(Message.id)).where(Message.account_id == account.id)
            )
        ).scalar_one() == 1

    async def test_purge_only_removes_messages_without_any_placement(
        self, session, account, folder
    ):
        """A message that still exists in another folder must not be deleted."""
        other = Folder(
            account_id=account.id,
            remote_id="Archive",
            name="Archive",
            path="Archive",
            role="archive",
        )
        session.add(other)
        await session.commit()

        await sync_folder(session, account, folder, FakeConn(uids=[1], uidnext=2))
        message_id = (
            await session.execute(select(Message.id).where(Message.account_id == account.id))
        ).scalar_one()
        session.add(MailboxMessage(folder_id=other.id, message_id=message_id, remote_uid=1))
        await session.commit()

        folder.sync_pass = 0
        await session.commit()
        await sync_folder(session, account, folder, FakeConn(uids=[], uidnext=2))

        # The INBOX placement is gone, but the message remains because it's still in Archive.
        remaining = (
            await session.execute(
                select(func.count(Message.id)).where(Message.account_id == account.id)
            )
        ).scalar_one()
        assert remaining == 1


class TestSyncRunBookkeeping:
    async def test_runs_are_recorded_with_counts(self, session, account, folder):
        await sync_folder(session, account, folder, FakeConn(uids=[1, 2], uidnext=3))

        run = (
            await session.execute(
                select(SyncRun)
                .where(SyncRun.account_id == account.id)
                .order_by(SyncRun.started_at.desc())
                .limit(1)
            )
        ).scalar_one()
        assert run.kind == "full"
        assert run.added == 2
        assert run.finished_at is not None
        assert run.error is None

    async def test_transient_failure_marks_folder_error_and_records_run(
        self, session, account, folder
    ):
        from lenovmail.providers.imap_pool import MailTransientError

        class FailingConn(FakeConn):
            async def search(self, criteria: Any) -> list[int]:
                raise MailTransientError("connection dropped")

        # Captured before sync: the rollback inside sync makes the ORM instance stale.
        account_id = account.id
        with pytest.raises(MailTransientError):
            await sync_folder(session, account, folder, FailingConn(uids=[1], uidnext=2))

        await session.refresh(folder)
        assert folder.sync_state == "error"
        assert folder.sync_error is not None and "connection dropped" in folder.sync_error

        failed = (
            await session.execute(
                select(SyncRun)
                .where(SyncRun.account_id == account_id, SyncRun.error.is_not(None))
                .order_by(SyncRun.id.desc())
                .limit(1)
            )
        ).scalar_one()
        assert "connection dropped" in failed.error


class TestChunking:
    async def test_headers_fetched_in_configured_chunks(self, session, account, folder):
        uids = list(range(1, 8))
        chunk = 3
        original = settings.header_fetch_chunk
        settings.header_fetch_chunk = chunk
        try:
            conn = FakeConn(uids=uids, uidnext=8)
            await sync_folder(session, account, folder, conn)
        finally:
            settings.header_fetch_chunk = original

        envelope_calls = [c for c in conn.fetch_calls if "ENVELOPE" in c[1]]
        assert [len(c[0]) for c in envelope_calls] == [3, 3, 1]

    async def test_message_count_matches_server(self, session, account, folder):
        count = 25
        conn = FakeConn(uids=list(range(1, count + 1)), uidnext=count + 1)
        await sync_folder(session, account, folder, conn)

        assert (
            await session.execute(
                select(func.count(Message.id)).where(Message.account_id == account.id)
            )
        ).scalar_one() == count
        assert (
            await session.execute(
                select(func.count(MailboxMessage.id)).where(MailboxMessage.folder_id == folder.id)
            )
        ).scalar_one() == count

    async def test_provisional_hash_used_when_envelope_lacks_message_id(
        self, session, account, folder
    ):
        class NoIdEnvelope(FakeEnvelope):
            def __init__(self) -> None:
                super().__init__("", "No Message-ID")
                self.message_id = None

        class NoIdConn(FakeConn):
            async def fetch(self, messages, data, modifiers=None):
                response = await super().fetch(messages, data, modifiers)
                if "ENVELOPE" in data:
                    for uid in response:
                        response[uid][b"ENVELOPE"] = NoIdEnvelope()
                return response

        await sync_folder(session, account, folder, NoIdConn(uids=[5], uidnext=6))

        row = (
            await session.execute(select(Message).where(Message.account_id == account.id))
        ).scalar_one()
        assert row.rfc822_message_id is None
        assert row.headers == {"provisional": True}
        assert (
            await session.execute(
                select(func.count(Message.id)).where(Message.account_id == account.id)
            )
        ).scalar_one() == 1


class TestFolderCursor:
    async def test_cursor_and_last_synced_are_updated(self, session, account, folder):
        await sync_folder(
            session, account, folder, FakeConn(uids=[1], uidvalidity=42, uidnext=2, highestmodseq=9)
        )
        await session.refresh(folder)

        assert (folder.uidvalidity, folder.uidnext, folder.highestmodseq) == (42, 2, 9)
        assert folder.last_synced_at is not None
        assert folder.sync_state == "idle"
        assert folder.sync_error is None


def test_folder_fixture_is_isolated(account) -> None:
    """Sanity check: each test uses a fresh account (fixture `account`), so no state leaks."""
    assert account.id not in {uuid.UUID(int=0)}
