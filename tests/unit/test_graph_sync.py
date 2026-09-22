# Lenovmail — authored by satuapps
"""Microsoft Graph path tests: folder/message delta, deletion, and body fallback.

An M365 tenant is not available in the test environment, so `GraphClient` is replaced
with a fake client that records requests and serves a pre-arranged sequence of delta
responses. What's tested here are the decisions that are easy to get wrong: expired
delta tokens, `@removed` versus "not mentioned in delta", and `/$value` refusal.
"""

from __future__ import annotations

from email.message import EmailMessage
from typing import Any

import pytest
from sqlalchemy import func, select

from lenovmail import blobs
from lenovmail.config import settings
from lenovmail.models import Attachment, Folder, GraphSettings, MailboxMessage, Message
from lenovmail.providers.graph import GraphRequestError
from lenovmail.sync.graph_sync import fetch_bodies, sync_folder, sync_folders

FOLDERS_URL = "https://graph.microsoft.com/v1.0/me/mailFolders/delta?$deltatoken=1"


def item(
    item_id: str,
    *,
    subject: str = "Message",
    is_read: bool = False,
    has_attachments: bool = False,
    message_id: str | None = None,
    received: str = "2026-09-20T10:00:00Z",
) -> dict[str, Any]:
    return {
        "id": item_id,
        "internetMessageId": message_id or f"<{item_id}@example.com>",
        "subject": subject,
        "receivedDateTime": received,
        "bodyPreview": f"snippet {subject}",
        "isRead": is_read,
        "isDraft": False,
        "hasAttachments": has_attachments,
        "from": {"emailAddress": {"name": "Budi", "address": "Budi@Example.com"}},
        "toRecipients": [{"emailAddress": {"name": "Siti", "address": "siti@example.org"}}],
        "flag": {"flagStatus": "notFlagged"},
    }


def removed(item_id: str) -> dict[str, Any]:
    return {"id": item_id, "@removed": {"reason": "deleted"}}


class FakeGraphClient:
    """Fake Graph client with a per-folder sequence of delta responses."""

    def __init__(
        self,
        *,
        folders: list[dict[str, Any]] | None = None,
        deltas: dict[str, list[list[dict[str, Any]]]] | None = None,
        mime: dict[str, bytes | Exception] | None = None,
        messages: dict[str, dict[str, Any]] | None = None,
        attachments: dict[str, list[dict[str, Any]]] | None = None,
        expire: set[str] | None = None,
    ) -> None:
        self.folders = folders or []
        self.deltas = {key: list(value) for key, value in (deltas or {}).items()}
        self.mime = mime or {}
        self.messages = messages or {}
        self.attachments = attachments or {}
        # Folder (or "__folders__") that answers 410 once, then succeeds.
        self.expire = set(expire or ())
        self.folder_calls: list[str | None] = []
        self.message_calls: list[tuple[str, str | None]] = []
        self.mime_calls: list[str] = []
        self._seq = 0

    def _delta(self, folder_id: str, link: str | None) -> tuple[list[dict[str, Any]], str]:
        self._seq += 1
        payloads = self.deltas.get(folder_id) or [[]]
        payload = payloads.pop(0) if len(payloads) > 1 else payloads[0]
        return payload, f"https://graph.microsoft.com/v1.0/delta?token={self._seq}"

    def _maybe_expire(self, key: str) -> None:
        if key in self.expire:
            self.expire.discard(key)
            raise GraphRequestError(410, "syncStateNotFound", "delta token expired")

    async def folders_delta(self, link: str | None = None) -> tuple[list[dict[str, Any]], str]:
        self.folder_calls.append(link)
        if link is not None:
            self._maybe_expire("__folders__")
        return self.folders, FOLDERS_URL

    async def messages_delta(
        self, remote_folder_id: str, link: str | None = None
    ) -> tuple[list[dict[str, Any]], str]:
        self.message_calls.append((remote_folder_id, link))
        if link is not None:
            self._maybe_expire(remote_folder_id)
        return self._delta(remote_folder_id, link)

    async def message_mime(self, message_id: str) -> bytes:
        self.mime_calls.append(message_id)
        payload = self.mime.get(message_id)
        if isinstance(payload, Exception):
            raise payload
        if payload is None:
            raise GraphRequestError(404, "ErrorItemNotFound", "not found")
        return payload

    async def message(
        self, message_id: str, *, html_body: bool = False, select: str | None = None
    ) -> dict[str, Any]:
        payload = self.messages.get(message_id)
        if payload is None:
            raise GraphRequestError(404, "ErrorItemNotFound", "not found")
        return payload

    async def message_attachments(self, message_id: str) -> list[dict[str, Any]]:
        return self.attachments.get(message_id, [])


def raw_mail(
    *, subject: str = "Monthly report", body: str = "Report content", attachment: bool = False
) -> bytes:
    message = EmailMessage()
    message["From"] = "Budi <budi@example.com>"
    message["To"] = "siti@example.org"
    message["Subject"] = subject
    message["Date"] = "Sun, 20 Sep 2026 10:00:00 +0000"
    message["Message-ID"] = "<mime-1@example.com>"
    message.set_content(body)
    if attachment:
        message.add_attachment(
            b"report", maintype="application", subtype="pdf", filename="report.pdf"
        )
    return message.as_bytes()


async def placements(session, folder: Folder) -> list[tuple[str, bool]]:
    rows = (
        await session.execute(
            select(MailboxMessage.remote_item_id, MailboxMessage.flag_seen)
            .where(MailboxMessage.folder_id == folder.id)
            .order_by(MailboxMessage.remote_item_id)
        )
    ).all()
    return [(row[0], row[1]) for row in rows]


class TestFolderDiscovery:
    async def test_roles_hierarchy_and_delta_link_are_persisted(self, session, account):
        client = FakeGraphClient(
            folders=[
                {
                    "id": "f-inbox",
                    "displayName": "Inbox",
                    "wellKnownName": "inbox",
                    "parentFolderId": "f-root",
                },
                {
                    "id": "f-sent",
                    "displayName": "Sent Items",
                    "wellKnownName": "sentitems",
                    "parentFolderId": "f-root",
                },
                {
                    "id": "f-project",
                    "displayName": "Project Alpha",
                    "parentFolderId": "f-inbox",
                },
                {"id": "f-root", "displayName": "Root", "wellKnownName": "msgfolderroot"},
            ]
        )
        folders = await sync_folders(session, account, client)
        roles = {row.remote_id: row.role for row in folders}
        assert roles["f-inbox"] == "inbox"
        assert roles["f-sent"] == "sent"
        assert roles["f-project"] == "other"

        parent_of_project = (
            await session.execute(select(Folder.parent_id).where(Folder.remote_id == "f-project"))
        ).scalar_one()
        assert parent_of_project == next(row.id for row in folders if row.remote_id == "f-inbox")

        stored = (
            await session.execute(
                select(GraphSettings.folders_delta_link_enc).where(
                    GraphSettings.account_id == account.id
                )
            )
        ).scalar_one()
        assert stored

        # The next cycle uses the stored token, not a full enumeration.
        await sync_folders(session, account, client)
        assert client.folder_calls[0] is None
        assert client.folder_calls[1] is not None

    async def test_expired_folder_delta_restarts_from_scratch(self, session, account):
        client = FakeGraphClient(
            folders=[{"id": "f-inbox", "displayName": "Inbox", "wellKnownName": "inbox"}],
            expire={"__folders__"},
        )
        await sync_folders(session, account, client)
        await sync_folders(session, account, client)

        # The stored token is used, refused with 410, then retried as a full enumeration.
        assert client.folder_calls[0] is None
        assert client.folder_calls[1] is not None
        assert client.folder_calls[2] is None
        assert (
            len(
                (await session.execute(select(Folder).where(Folder.account_id == account.id)))
                .scalars()
                .all()
            )
            == 1
        )


class TestMessagesDelta:
    async def test_delta_writes_placements_and_resync_does_not_duplicate(
        self, session, account, folder
    ):
        client = FakeGraphClient(
            deltas={
                "INBOX": [
                    [item("m1", subject="Invoice"), item("m2", subject="Meeting")],
                    [item("m1", subject="Invoice", is_read=True)],
                ]
            }
        )
        first = await sync_folder(session, account, folder, client)
        assert first.added == 2 and first.kind == "full"
        assert await placements(session, folder) == [("m1", False), ("m2", False)]

        await session.refresh(folder)
        second = await sync_folder(session, account, folder, client)
        assert second.kind == "incremental"
        assert second.updated == 1 and second.added == 0
        # One row per message: the same delta must not add a new placement.
        assert await placements(session, folder) == [("m1", True), ("m2", False)]
        assert (
            await session.execute(
                select(func.count(Message.id)).where(Message.account_id == account.id)
            )
        ).scalar_one() == 2

    async def test_removed_entry_drops_placement_and_purges_message(self, session, account, folder):
        client = FakeGraphClient(deltas={"INBOX": [[item("m1"), item("m2")], [removed("m1")]]})
        await sync_folder(session, account, folder, client)
        await session.refresh(folder)
        stats = await sync_folder(session, account, folder, client)

        assert stats.removed == 1
        assert [remote_id for remote_id, _ in await placements(session, folder)] == ["m2"]
        # Orphaned messages get cleaned up too since they have no other placement.
        assert (
            await session.execute(
                select(func.count(Message.id)).where(Message.account_id == account.id)
            )
        ).scalar_one() == 1

    async def test_items_absent_from_incremental_delta_are_kept(self, session, account, folder):
        # Incremental delta only mentions what changed: m2 must not be treated as deleted.
        client = FakeGraphClient(
            deltas={"INBOX": [[item("m1"), item("m2")], [item("m1", is_read=True)]]}
        )
        await sync_folder(session, account, folder, client)
        await session.refresh(folder)
        stats = await sync_folder(session, account, folder, client)

        assert stats.removed == 0
        assert [remote_id for remote_id, _ in await placements(session, folder)] == ["m1", "m2"]

    async def test_expired_delta_runs_full_sync_and_prunes_stale_placements(
        self, session, account, folder
    ):
        client = FakeGraphClient(
            deltas={"INBOX": [[item("m1"), item("m2")], [item("m2")]]},
            expire={"INBOX"},
        )
        await sync_folder(session, account, folder, client)
        await session.refresh(folder)
        stats = await sync_folder(session, account, folder, client)

        assert stats.full_resync is True and stats.kind == "full"
        # Attempt with the old token, then retry without a token.
        assert client.message_calls[-2][1] is not None
        assert client.message_calls[-1][1] is None
        # m1 is absent from the full delta -> placement gone, orphaned message cleaned up.
        assert [remote_id for remote_id, _ in await placements(session, folder)] == ["m2"]
        assert (
            await session.execute(
                select(func.count(Message.id)).where(Message.account_id == account.id)
            )
        ).scalar_one() == 1


class TestBodies:
    async def test_mime_body_is_parsed_stored_and_searchable(
        self, session, account, folder, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(settings, "blob_root", tmp_path / "blobs")
        client = FakeGraphClient(
            deltas={"INBOX": [[item("m1", subject="Monthly report")]]},
            mime={"m1": raw_mail(attachment=True)},
        )
        await sync_folder(session, account, folder, client)
        stored = await fetch_bodies(session, account, folder, client)

        assert stored == 1
        row = (
            await session.execute(select(Message).where(Message.account_id == account.id))
        ).scalar_one()
        assert row.body_state == "full"
        assert row.has_attachments is True
        assert row.blob_sha256 is not None
        assert await blobs.exists(row.blob_sha256) is True

        attachment = (
            await session.execute(select(Attachment).where(Attachment.message_id == row.id))
        ).scalar_one()
        assert attachment.filename == "report.pdf"
        assert attachment.mime_type == "application/pdf"

    async def test_refused_value_falls_back_to_json_metadata(
        self, session, account, folder, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(settings, "blob_root", tmp_path / "blobs")
        client = FakeGraphClient(
            deltas={"INBOX": [[item("m1", subject="Large", has_attachments=True)]]},
            mime={"m1": GraphRequestError(400, "ErrorInvalidIdMalformed", "$value refused")},
            messages={
                "m1": {
                    **item("m1", subject="Large", has_attachments=True),
                    "body": {"contentType": "html", "content": "<p>Hello <b>world</b></p>"},
                }
            },
            attachments={
                "m1": [
                    {
                        "id": "att-1",
                        "name": "large.zip",
                        "contentType": "application/zip",
                        "size": 4096,
                        "isInline": False,
                    }
                ]
            },
        )
        await sync_folder(session, account, folder, client)
        stored = await fetch_bodies(session, account, folder, client)

        assert stored == 1
        row = (
            await session.execute(select(Message).where(Message.account_id == account.id))
        ).scalar_one()
        assert row.body_state == "partial"
        assert row.size_bytes is None
        body = (await session.execute(select(Message).where(Message.id == row.id))).scalar_one()
        assert body.headers == {"partial": "graph"}

        attachment = (
            await session.execute(select(Attachment).where(Attachment.message_id == row.id))
        ).scalar_one()
        assert attachment.filename == "large.zip"
        assert attachment.size_bytes == 4096

    async def test_transient_error_propagates_without_marking_partial(
        self, session, account, folder, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(settings, "blob_root", tmp_path / "blobs")
        client = FakeGraphClient(
            deltas={"INBOX": [[item("m1")]]},
            mime={"m1": GraphRequestError(403, "ErrorAccessDenied", "denied")},
        )
        await sync_folder(session, account, folder, client)
        with pytest.raises(GraphRequestError):
            await fetch_bodies(session, account, folder, client)

        row = (
            await session.execute(select(Message).where(Message.account_id == account.id))
        ).scalar_one()
        # A permission failure isn't "message unreadable": the status still stays pending fetch.
        assert row.body_state == "none"
