# Lenovmail — authored by satuapps (satuapps.com)
"""Attachment byte retrieval — used by the REST download route and the MCP tool.

Attachment bytes are not stored separately: for messages that have a MIME blob, the
part is extracted from that blob by `part_path`. Graph messages whose body was fetched
via JSON (no blob) pull their content directly from Graph.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import blobs
from ..models import Account, Attachment, MailboxMessage, Message
from ..providers.accounts import graph_client_for, save_graph_tokens
from .normalize import extract_part


class AttachmentUnavailableError(Exception):
    """Attachment content could not be retrieved from any source."""


async def account_of(session: AsyncSession, message: Message) -> Account:
    account = await session.get(Account, message.account_id)
    if account is None:  # pragma: no cover - enforced by the caller
        raise AttachmentUnavailableError("message account not found")
    return account


async def load_attachment_bytes(
    session: AsyncSession, message: Message, attachment: Attachment
) -> tuple[bytes, str]:
    """Return (bytes, mime_type); raises `AttachmentUnavailableError` when unavailable."""
    mime_type = attachment.mime_type or "application/octet-stream"

    if message.blob_sha256 is not None:
        raw = await blobs.get(message.blob_sha256)
        if raw is None:
            raise AttachmentUnavailableError("message blob missing")
        extracted = extract_part(raw, attachment.part_path)
        if extracted is None:
            raise AttachmentUnavailableError("attachment part not found in message")
        data, detected = extracted
        return data, attachment.mime_type or detected or mime_type

    account = await account_of(session, message)
    if account.provider != "graph":
        raise AttachmentUnavailableError("this attachment is not stored locally")
    placement = (
        await session.execute(
            select(MailboxMessage.remote_item_id)
            .where(
                MailboxMessage.message_id == message.id,
                MailboxMessage.remote_item_id.is_not(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if placement is None:
        raise AttachmentUnavailableError("Graph item for this message does not exist")
    client = await graph_client_for(session, account)
    try:
        data = await client.attachment_bytes(placement, attachment.part_path)
        await save_graph_tokens(session, account.id, client)
    finally:
        await client.aclose()
    return data, mime_type
