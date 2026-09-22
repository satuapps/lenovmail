# Lenovmail — authored by satuapps (satuapps.com)
"""Outbound email queue: build MIME, store `outbox` rows, and send per account.

Accounts with `provider='imap'` are sent via `providers.smtp` then (optionally) copied to
the Sent folder via `providers.imap_ops.append`. Accounts with `provider='graph'` are sent
via `GraphClient.send_mail` (same token-cache pattern as `sync/actions.py`:
`graph_client_for` + `save_graph_tokens` + `client.aclose()`).

Provider errors are mapped to `MailAuthError`/`MailTransientError`/`MailPermanentError`
(defined in `providers.imap_pool`, but protocol-neutral — also used for Graph):
`MailTransientError` returns the row to `queued` for a retry; `MailAuthError` and
`MailPermanentError` mark it `failed`. Both failure kinds increment `attempts`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..logging import get_logger
from ..models import Account, AgentToken, Folder, Outbox
from ..providers import imap_ops as ops
from ..providers.accounts import (
    AccountConfigError,
    get_account_pool,
    graph_client_for,
    load_smtp_config,
    save_graph_tokens,
)
from ..providers.graph import GraphAuthError, GraphRequestError, GraphTransientError
from ..providers.imap_pool import MailAuthError, MailPermanentError, MailTransientError
from ..providers.smtp import SmtpConfig
from ..providers.smtp import send_message as smtp_send_message
from . import store

# A `sending` claim older than this is considered stale (the process died mid-send).
CLAIM_TIMEOUT_S = 900


class SendLimitExceeded(Exception):
    """Agent token has already used up its send allowance for the last hour."""


async def enforce_agent_send_limit(session: AsyncSession, agent: AgentToken) -> None:
    """Reject sending if the token has exceeded `send_limit_per_hour` (computed from outbox)."""
    since = datetime.now(UTC) - timedelta(hours=1)
    used = (
        await session.execute(
            select(func.count(Outbox.id)).where(
                Outbox.created_by_token_id == agent.id, Outbox.created_at >= since
            )
        )
    ).scalar_one()
    if used >= agent.send_limit_per_hour:
        raise SendLimitExceeded(
            f"agent token send limit reached ({used}/{agent.send_limit_per_hour} per hour)"
        )


log = get_logger(__name__)


def _build_mime_sync(payload: dict[str, Any]) -> bytes:
    msg = EmailMessage()
    msg["From"] = payload["from"]
    msg["To"] = ", ".join(payload.get("to") or [])
    if payload.get("cc"):
        msg["Cc"] = ", ".join(payload["cc"])
    # Bcc is deliberately not written to the header — it is only used as the envelope
    # recipient list when sending.
    msg["Subject"] = payload.get("subject") or ""
    msg["Date"] = format_datetime(datetime.now(UTC))
    msg["Message-ID"] = make_msgid()
    if payload.get("in_reply_to"):
        msg["In-Reply-To"] = payload["in_reply_to"]
    references = payload.get("references") or []
    if references:
        msg["References"] = " ".join(references)

    text = payload.get("text")
    html = payload.get("html")
    if text and html:
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
    elif html:
        msg.set_content(html, subtype="html")
    else:
        msg.set_content(text or "")

    for attachment in payload.get("attachments") or []:
        data = base64.b64decode(attachment["content_b64"])
        mime_type = attachment.get("mime_type") or "application/octet-stream"
        maintype, _, subtype = mime_type.partition("/")
        msg.add_attachment(
            data,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=attachment["filename"],
        )
    return msg.as_bytes()


async def build_mime(payload: dict[str, Any]) -> bytes:
    """Build the RFC822 message from the outbox payload (fixed shape lives in `sync/outbox.py`).

    Runs in a thread: encoding large attachments must not block the event loop.
    """
    return await asyncio.to_thread(_build_mime_sync, payload)


async def queue(
    session: AsyncSession,
    account: Account,
    *,
    payload: dict[str, Any],
    created_by_user_id: Any = None,
    created_by_token_id: Any = None,
    requires_approval: bool = False,
) -> Outbox:
    """Store a new message in `outbox`. Initial status is `pending_approval` or `queued`."""
    raw = await build_mime(payload)
    row = Outbox(
        account_id=account.id,
        created_by_user_id=created_by_user_id,
        created_by_token_id=created_by_token_id,
        status="pending_approval" if requires_approval else "queued",
        payload=payload,
        mime_sha256=hashlib.sha256(raw).digest(),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def approve(session: AsyncSession, row: Outbox) -> Outbox:
    """Approve a `pending_approval` row so the next `send_pending` picks it up."""
    row.status = "queued"
    await session.commit()
    await session.refresh(row)
    return row


def _envelope_recipients(payload: dict[str, Any]) -> list[str]:
    return [
        *(payload.get("to") or []),
        *(payload.get("cc") or []),
        *(payload.get("bcc") or []),
    ]


async def _append_to_sent(session: AsyncSession, account: Account, raw: bytes) -> None:
    """Copy the sent message to the Sent folder. Failures are only logged — the message
    has already been sent via SMTP, so do not mark it failed just because the local copy
    was not stored."""
    folder = (
        (
            await session.execute(
                select(Folder).where(Folder.account_id == account.id, Folder.role == "sent")
            )
        )
        .scalars()
        .first()
    )
    if folder is None:
        return
    try:
        pool = await get_account_pool(session, account)
        async with pool.connection() as conn:
            await ops.append(conn, folder.path or folder.name, raw, seen=True)
    except Exception:
        log.warning("append_to_sent_failed", account_id=str(account.id), exc_info=True)


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


async def _send_via_imap(
    session: AsyncSession, account: Account, payload: dict[str, Any], raw: bytes
) -> None:
    try:
        smtp_settings = await load_smtp_config(session, account)
    except AccountConfigError as exc:
        raise MailPermanentError(str(exc)) from exc

    port_value = smtp_settings["port"]
    config = SmtpConfig(
        host=str(smtp_settings["host"]),
        port=port_value if isinstance(port_value, int) else int(str(port_value)),
        security=str(smtp_settings["security"]),
        username=_optional_str(smtp_settings.get("username")),
        password=_optional_str(smtp_settings.get("password")),
    )
    await smtp_send_message(
        config, raw, envelope_from=payload["from"], recipients=_envelope_recipients(payload)
    )
    if smtp_settings.get("append_to_sent"):
        await _append_to_sent(session, account, raw)


def _graph_addresses(addrs: list[str]) -> list[dict[str, dict[str, str]]]:
    return [{"emailAddress": {"address": addr}} for addr in addrs]


def _graph_message_payload(payload: dict[str, Any]) -> dict[str, Any]:
    html = payload.get("html")
    text = payload.get("text")
    body = (
        {"contentType": "HTML", "content": html}
        if html
        else {
            "contentType": "Text",
            "content": text or "",
        }
    )
    message: dict[str, Any] = {
        "subject": payload.get("subject") or "",
        "body": body,
        "toRecipients": _graph_addresses(payload.get("to") or []),
    }
    if payload.get("cc"):
        message["ccRecipients"] = _graph_addresses(payload["cc"])
    if payload.get("bcc"):
        message["bccRecipients"] = _graph_addresses(payload["bcc"])
    attachments = payload.get("attachments") or []
    if attachments:
        message["attachments"] = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": attachment["filename"],
                "contentType": attachment.get("mime_type") or "application/octet-stream",
                "contentBytes": attachment["content_b64"],
            }
            for attachment in attachments
        ]
    return message


async def _send_via_graph(session: AsyncSession, account: Account, payload: dict[str, Any]) -> None:
    client = await graph_client_for(session, account)
    try:
        await client.send_mail(_graph_message_payload(payload))
        await save_graph_tokens(session, account.id, client)
    except GraphAuthError as exc:
        raise MailAuthError(str(exc)) from exc
    except GraphTransientError as exc:
        raise MailTransientError(str(exc)) from exc
    except GraphRequestError as exc:
        raise MailPermanentError(str(exc)) from exc
    finally:
        await client.aclose()


async def send_one(session: AsyncSession, account: Account, row: Outbox) -> Outbox:
    """Send a single outbox row. Always commits; the final status reflects the real outcome."""
    row.status = "sending"
    await session.commit()

    try:
        raw = await build_mime(row.payload)
        if account.provider == "graph":
            await _send_via_graph(session, account, row.payload)
        else:
            await _send_via_imap(session, account, row.payload, raw)
    except MailTransientError as exc:
        row.status = "queued"
        row.attempts += 1
        row.last_error = str(exc)[:2000]
    except (MailAuthError, MailPermanentError) as exc:
        row.status = "failed"
        row.attempts += 1
        row.last_error = str(exc)[:2000]
    else:
        row.status = "sent"
        row.sent_at = datetime.now(UTC)

    await session.commit()
    return row


async def send_pending(session: AsyncSession, account: Account, limit: int = 10) -> int:
    """Send the oldest `queued` rows first; return the count of rows sent successfully.

    Rows are **claimed** first (`status='sending'` + `locked_at`) in a single transaction
    with `FOR UPDATE SKIP LOCKED`, then sent. Without the claim step, two concurrent
    sender jobs would pick the same row and send it twice.
    """
    await _release_stale_claims(session, account.id)

    rows = (
        (
            await session.execute(
                select(Outbox)
                .where(Outbox.account_id == account.id, Outbox.status == "queued")
                .order_by(Outbox.created_at.asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return 0

    now = datetime.now(UTC)
    claimed: list[uuid.UUID] = []
    for row in rows:
        row.status = "sending"
        row.locked_at = now
        claimed.append(row.id)
    await session.commit()

    sent = 0
    for row_id in claimed:
        claimed_row = await session.get(Outbox, row_id)
        if claimed_row is None:
            continue
        result = await send_one(session, account, claimed_row)
        if result.status == "sent":
            sent += 1
    return sent


async def _release_stale_claims(
    session: AsyncSession, account_id: uuid.UUID, timeout_s: int = CLAIM_TIMEOUT_S
) -> int:
    """Return `sending` rows whose claim has gone stale back to `queued`.

    Happens when the sender process dies after claiming but before finishing; without
    this recovery, the message would never be sent.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=timeout_s)
    released = await store.execute_count(
        session,
        update(Outbox)
        .where(
            Outbox.account_id == account_id,
            Outbox.status == "sending",
            Outbox.locked_at.is_not(None),
            Outbox.locked_at < cutoff,
        )
        .values(status="queued", locked_at=None, last_error="send claim expired"),
    )
    await session.commit()
    return released
