# Lenovmail — authored by satuapps
"""E2E IMAP tests against a real mail server (GreenMail in docker-compose.test.yml).

Automatically skipped when the test IMAP port is not open, so `pytest` stays green on
machines without the container. Exercises the full path: encrypted credentials -> connection
pool -> IMAP operations -> sync engine -> MIME normalization -> threading -> Postgres ->
full-text search.

Deliberately included: this server does **not** advertise SPECIAL-USE or CONDSTORE, so the
folder-name heuristic path and the full flag refresh are also exercised.
"""

from __future__ import annotations

import contextlib
import hashlib
import socket
import uuid
from email.message import EmailMessage

import pytest
from sqlalchemy import func, select

from lenovmail import blobs
from lenovmail.config import settings
from lenovmail.crypto import account_aad, encrypt
from lenovmail.models import Attachment, Folder, ImapSettings, Message, MessageBody
from lenovmail.providers import imap_ops as ops
from lenovmail.providers.imap_pool import AccountImapConfig, get_pool, reset_pool
from lenovmail.search import tsquery_expr
from lenovmail.sync.imap_sync import fetch_bodies, sync_folder, sync_folders
from lenovmail.sync.normalize import parse_message

from ..conftest import TEST_IMAP_PORT

IMAP_HOST = "127.0.0.1"
IMAP_USER = "demo"
IMAP_PASSWORD = "demo"
SMTP_PORT = 3025


def port_open(port: int, host: str = IMAP_HOST) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not port_open(TEST_IMAP_PORT),
    reason="GreenMail is not running (docker compose -f docker-compose.test.yml up -d)",
)


def simple_message(subject: str, body: str, message_id: str | None = None) -> bytes:
    msg = EmailMessage()
    msg["From"] = "John Carter <john@example.com>"
    msg["To"] = "demo@lenov.test"
    msg["Subject"] = subject
    msg["Date"] = "Mon, 21 Sep 2026 10:00:00 +0700"
    if message_id:
        msg["Message-ID"] = message_id
    msg.set_content(body)
    return msg.as_bytes()


def attachment_message(subject: str, filename: str, payload: bytes, message_id: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = "Emma <emma@example.org>"
    msg["To"] = "demo@lenov.test"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = "Mon, 21 Sep 2026 11:00:00 +0700"
    msg.set_content("see attachment")
    msg.add_attachment(payload, maintype="application", subtype="pdf", filename=filename)
    return msg.as_bytes()


def reply_message(subject: str, parent_id: str, message_id: str) -> bytes:
    msg = EmailMessage()
    msg["From"] = "John Carter <john@example.com>"
    msg["To"] = "demo@lenov.test"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["In-Reply-To"] = parent_id
    msg["References"] = parent_id
    msg["Date"] = "Mon, 21 Sep 2026 12:00:00 +0700"
    msg.set_content("reply")
    return msg.as_bytes()


@pytest.fixture
async def wired_account(session, account, monkeypatch, tmp_path):
    """Account + folder on the test server, with encrypted credentials as in production."""
    monkeypatch.setattr(settings, "blob_root", tmp_path / "blobs")
    monkeypatch.setattr(settings, "body_backfill_max_per_run", 500)

    mailbox = f"LenovmailTest{uuid.uuid4().hex[:8]}"
    session.add(
        ImapSettings(
            account_id=account.id,
            host=IMAP_HOST,
            port=TEST_IMAP_PORT,
            security="none",
            username=IMAP_USER,
            password_enc=encrypt(account_aad(account.id, "password_enc"), IMAP_PASSWORD.encode()),
            smtp_host=IMAP_HOST,
            smtp_port=SMTP_PORT,
            smtp_security="none",
            smtp_username=IMAP_USER,
        )
    )
    folder = Folder(
        account_id=account.id,
        remote_id=mailbox,
        name=mailbox,
        path=mailbox,
        role="inbox",
    )
    session.add(folder)
    await session.commit()

    pool = await get_pool(
        str(account.id),
        AccountImapConfig(
            host=IMAP_HOST,
            port=TEST_IMAP_PORT,
            security="none",
            username=IMAP_USER,
            password=IMAP_PASSWORD,
        ),
    )
    async with pool.connection() as conn:
        with contextlib.suppress(Exception):
            await conn._call("create_folder", mailbox)

    yield account, folder, pool, mailbox

    await reset_pool(str(account.id))


async def append_to(conn, mailbox: str, raw: bytes) -> None:
    await ops.append(conn, mailbox, raw)


class TestFolderDiscovery:
    async def test_folder_discovery_is_idempotent(self, session, wired_account):
        """A new IMAP account must get a `folders` row before the sync cycle runs."""
        account, folder, pool, mailbox = wired_account
        account_id = account.id

        async with pool.connection() as conn:
            first = await sync_folders(session, account, conn)

        names = {row.name for row in first}
        assert mailbox in names, f"test folder {mailbox} not detected: {names}"
        assert "INBOX" in names
        inbox = next(row for row in first if row.name == "INBOX")
        assert inbox.role == "inbox", "INBOX role must be recognized from its name"

        stored = (
            (
                await session.execute(
                    select(func.count(Folder.id)).where(
                        Folder.account_id == account_id, Folder.remote_id == mailbox
                    )
                )
            ).scalar_one(),
        )[0]
        assert stored == 1

        async with pool.connection() as conn:
            second = await sync_folders(session, account, conn)

        assert len(second) == len(first), "re-detection must not duplicate folders"
        total = (
            await session.execute(
                select(func.count(Folder.id)).where(Folder.account_id == account_id)
            )
        ).scalar_one()
        assert total == len(first)


class TestEndToEndSync:
    async def test_full_sync_then_body_backfill_and_search(self, session, wired_account):
        account, folder, pool, mailbox = wired_account
        raw_plain = simple_message(
            "Invoice ACME 42", "payment pending this month", "<inv-42@example.com>"
        )
        raw_attach = attachment_message(
            "Q3 Report", "report.pdf", b"%PDF-1.4 dummy content", "<report-q3@example.com>"
        )
        raw_reply = reply_message(
            "Re: Q3 Report", "<report-q3@example.com>", "<q3-reply@example.com>"
        )

        async with pool.connection() as conn:
            for raw in (raw_plain, raw_attach, raw_reply):
                await append_to(conn, mailbox, raw)

            stats = await sync_folder(session, account, folder, conn)
            assert stats.kind == "full"
            assert stats.added == 3
            assert stats.body_candidates == 3

            # Header pass alone already populates the message list.
            count = (
                await session.execute(
                    select(func.count(Message.id)).where(Message.account_id == account.id)
                )
            ).scalar_one()
            assert count == 3

            stored = await fetch_bodies(session, account, folder, conn, limit=10)
            assert stored == 3

        # Body, snippet, and attachment are stored.
        invoice = (
            await session.execute(
                select(Message).where(
                    Message.account_id == account.id,
                    Message.rfc822_message_id == "<inv-42@example.com>",
                )
            )
        ).scalar_one()
        body = (
            await session.execute(select(MessageBody).where(MessageBody.message_id == invoice.id))
        ).scalar_one()
        assert body.body_text is not None and "payment pending" in body.body_text
        assert invoice.body_state == "full"
        assert invoice.blob_sha256 is not None
        assert invoice.snippet is not None
        assert invoice.from_addr == "john@example.com"

        report = (
            await session.execute(
                select(Message).where(
                    Message.account_id == account.id,
                    Message.rfc822_message_id == "<report-q3@example.com>",
                )
            )
        ).scalar_one()
        assert report.has_attachments is True
        attachment = (
            await session.execute(select(Attachment).where(Attachment.message_id == report.id))
        ).scalar_one()
        assert attachment.filename == "report.pdf"
        assert attachment.mime_type == "application/pdf"
        assert attachment.size_bytes == len(b"%PDF-1.4 dummy content")

        # Blob content-addressed: the stored digest matches the stored bytes,
        # and its content is semantically identical to the original message.
        #
        # Not compared byte-for-byte with `raw_attach`: IMAP APPEND normalizes
        # line endings (LF -> CRLF), so what actually gets addressed is the server's
        # bytes, not the local bytes before sending.
        stored_raw = await blobs.get(report.blob_sha256)

        assert hashlib.sha256(stored_raw).digest() == report.blob_sha256
        reparsed = parse_message(stored_raw)
        assert reparsed.rfc822_message_id == "<report-q3@example.com>"
        assert [a.filename for a in reparsed.attachments] == ["report.pdf"]
        assert reparsed.attachments[0].size_bytes == len(b"%PDF-1.4 dummy content")

        # Threading: the reply merges with its parent.
        reply = (
            await session.execute(
                select(Message).where(
                    Message.account_id == account.id,
                    Message.rfc822_message_id == "<q3-reply@example.com>",
                )
            )
        ).scalar_one()
        assert reply.thread_id == report.thread_id
        assert invoice.thread_id != report.thread_id

        # Full-text search reaches the body content (not just the subject).
        from lenovmail.models import MessageSearch

        hits = (
            (
                await session.execute(
                    select(Message.rfc822_message_id)
                    .join(MessageSearch, MessageSearch.message_id == Message.id)
                    .where(
                        Message.account_id == account.id,
                        MessageSearch.tsv.op("@@")(tsquery_expr("payment pending")),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert hits == ["<inv-42@example.com>"]

        # A word that only appears in the subject is also found.
        subject_hits = (
            (
                await session.execute(
                    select(Message.rfc822_message_id)
                    .join(MessageSearch, MessageSearch.message_id == Message.id)
                    .where(
                        Message.account_id == account.id,
                        MessageSearch.tsv.op("@@")(tsquery_expr("report")),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert set(subject_hits) == {"<report-q3@example.com>", "<q3-reply@example.com>"}

    async def test_incremental_sync_picks_up_only_new_messages(self, session, wired_account):
        account, folder, pool, mailbox = wired_account
        async with pool.connection() as conn:
            await append_to(conn, mailbox, simple_message("First", "content", "<m1@example.com>"))
            first = await sync_folder(session, account, folder, conn)
            assert first.added == 1

            await append_to(conn, mailbox, simple_message("Second", "content", "<m2@example.com>"))
            await session.refresh(folder)
            second = await sync_folder(session, account, folder, conn)

            assert second.kind == "incremental"
            assert second.added == 1
            # Test server without CONDSTORE: flags are only refreshed every FLAG_REFRESH_EVERY
            # cycles, so this cycle (pass 1) isn't due yet.
            assert second.flag_refresh is False

            subjects = set(
                (
                    await session.execute(
                        select(Message.subject).where(Message.account_id == account.id)
                    )
                )
                .scalars()
                .all()
            )
            assert subjects == {"First", "Second"}

    async def test_flag_change_from_client_is_synced_back(self, session, wired_account):
        """A flag changed by another client (e.g. read on a phone) is synced back."""
        account, folder, pool, mailbox = wired_account
        async with pool.connection() as conn:
            await append_to(conn, mailbox, simple_message("Flag", "content", "<flag@example.com>"))
            await sync_folder(session, account, folder, conn)

            uid = (
                await session.execute(
                    select(Message).where(
                        Message.account_id == account.id,
                        Message.rfc822_message_id == "<flag@example.com>",
                    )
                )
            ).scalar_one()
            del uid  # only confirms the message exists

            from lenovmail.models import MailboxMessage

            placement = (
                await session.execute(
                    select(MailboxMessage).where(MailboxMessage.folder_id == folder.id)
                )
            ).scalar_one()
            assert placement.flag_seen is False

            # Another client marks it read directly on the server.
            await ops.set_flags(conn, placement.remote_uid, add=(ops.FLAG_SEEN,))

            await session.refresh(folder)
            folder.sync_pass = 0  # force a flag-refresh cycle
            await session.commit()
            await sync_folder(session, account, folder, conn)

            flag_seen = (
                await session.execute(
                    select(MailboxMessage.flag_seen).where(MailboxMessage.id == placement.id)
                )
            ).scalar_one()
            assert flag_seen is True

    async def test_message_moved_away_on_server_is_dropped_locally(self, session, wired_account):
        account, folder, pool, mailbox = wired_account
        target = f"{mailbox}Archive"
        async with pool.connection() as conn:
            await conn._call("create_folder", target)
            await append_to(conn, mailbox, simple_message("Moved", "content", "<move@example.com>"))
            await sync_folder(session, account, folder, conn)

            from lenovmail.models import MailboxMessage

            placement = (
                await session.execute(
                    select(MailboxMessage).where(MailboxMessage.folder_id == folder.id)
                )
            ).scalar_one()
            await ops.move(conn, placement.remote_uid, target)

            await session.refresh(folder)
            folder.sync_pass = 0
            await session.commit()
            stats = await sync_folder(session, account, folder, conn)

            assert stats.removed == 1
            remaining = (
                await session.execute(
                    select(func.count(MailboxMessage.id)).where(
                        MailboxMessage.folder_id == folder.id
                    )
                )
            ).scalar_one()
            assert remaining == 0
            # The message no longer has a placement in this account -> its row is cleaned up.
            assert (
                await session.execute(
                    select(func.count(Message.id)).where(Message.account_id == account.id)
                )
            ).scalar_one() == 0
