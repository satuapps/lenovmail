# Lenovmail — authored by satuapps (satuapps.com)
"""MCP server: mail-management tools for AI agents.

Transport: Streamable HTTP, mounted on the API at `/api/mcp` and authenticated with the same
agent bearer token used by REST (`lnv_...`). Every tool checks the token's scope, account
ownership, and account limits; every call is logged to `agent_audit` under the tool's name.

Tools are intentionally thin: search uses `sync/queries.py`, write actions use
`sync/actions.py`, sending uses `sync/outbox.py`, and output shapes use the same API
serializers as the GUI — so agents and the GUI see data with identical meaning.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from arq.connections import ArqRedis
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import __version__
from ..api.serializers import (
    accounts_out,
    body_out,
    folders_out,
    message_flags,
    message_out,
    thread_out,
)
from ..config import settings
from ..db import SessionLocal
from ..logging import get_logger
from ..models import Account, AgentAudit, Attachment, Message, Outbox, Thread
from ..sync import actions
from ..sync import outbox as outbox_module
from ..sync.actions import FolderTargetError, MessageNotFoundError
from ..sync.attachments import AttachmentUnavailableError, load_attachment_bytes
from ..sync.queries import FolderNotFoundError, list_messages
from .auth import AgentPrincipal, AgentTokenVerifier, resolve_agent

log = get_logger(__name__)

MAX_ATTACHMENT_BYTES = 1_000_000  # cap on attachment content sent to the model

INSTRUCTIONS = """\
Lenovmail MCP server: manage the user's mail accounts (IMAP and Microsoft Graph).

Rules that apply to every tool:
- The bearer token determines scope (mail.read, mail.write, mail.send, mail.delete, mail.manage)
  and which accounts are reachable. Tools outside the token's scope are denied.
- Search uses full-text indexing (subject, body, sender, recipients); results are bounded by
  `limit` and continued with the `cursor` from the previous response.
- Sending always goes through the outbox queue: if the token requires approval, the message
  stops at `pending_approval` status until a human approves it in the GUI. Do not invent
  another way to send.
- Every call is logged to `agent_audit`.
"""

_arq: ArqRedis | None = None


def set_arq_pool(pool: ArqRedis | None) -> None:
    """Used by the API at startup: job queue for outbox delivery."""
    global _arq
    _arq = pool


async def _audit(
    principal: AgentPrincipal | None,
    tool: str,
    outcome: str,
    error: str | None,
    targets: list[str] | None,
) -> None:
    """Log a single tool call; audit failures never fail the tool."""
    try:
        async with SessionLocal() as session:
            session.add(
                AgentAudit(
                    token_id=principal.agent.id if principal else None,
                    user_id=principal.user.id if principal else None,
                    tool=f"mcp.{tool}",
                    target_ids=targets,
                    outcome=outcome,
                    error=(error or None) and error[:500],
                )
            )
            await session.commit()
    except Exception:  # noqa: BLE001 - audit must never fail the request
        log.warning("mcp_audit_failed", tool=tool, exc_info=True)


@asynccontextmanager
async def _agent_session(tool: str) -> AsyncIterator[tuple[AsyncSession, AgentPrincipal]]:
    """DB session + token principal for a single tool call, including audit logging."""
    access = get_access_token()
    if access is None:
        raise ToolError("request has no agent token")
    principal: AgentPrincipal | None = None
    outcome, error = "ok", None
    async with SessionLocal() as session:
        principal = await resolve_agent(session, access.token)
        try:
            yield session, principal
        except ToolError as exc:
            outcome, error = "denied", str(exc)
            raise
        except Exception as exc:  # noqa: BLE001 - reported to the agent as a tool error
            outcome, error = "error", f"{type(exc).__name__}: {exc}"
            raise
        finally:
            await _audit(principal, tool, outcome, error, None)


def _uuid(value: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ToolError(f"{label} is not a valid UUID: {value!r}") from exc


async def _message_of(session: AsyncSession, principal: AgentPrincipal, message_id: str) -> Message:
    """Message owned by an account within the token's scope, or `ToolError`."""
    message = await session.get(Message, _uuid(message_id, "message_id"))
    if message is None:
        raise ToolError("message not found")
    await principal.account(session, message.account_id)
    return message


_BASE_URL = settings.public_base_url.rstrip("/")

server: MCPServer = MCPServer(
    name="lenovmail",
    title="Lenovmail",
    version=__version__,
    instructions=INSTRUCTIONS,
    token_verifier=AgentTokenVerifier(),
    auth=AuthSettings(
        issuer_url=_BASE_URL,  # type: ignore[arg-type]
        resource_server_url=f"{_BASE_URL}/api/mcp",  # type: ignore[arg-type]
        required_scopes=[],
        # This opaque token (agent_tokens table) has no aud claim; the verifier already
        # checks its authenticity, so resource validation is explicitly disabled.
        validate_token_resource=False,
    ),
)


@server.tool(
    description="List the mail accounts reachable with this token, with status and message counts."
)
async def list_accounts() -> dict[str, Any]:
    async with _agent_session("list_accounts") as (session, principal):
        principal.require("mail.read")
        rows = (
            (
                await session.execute(
                    select(Account)
                    .where(Account.owner_id == principal.user.id)
                    .order_by(Account.email_address)
                )
            )
            .scalars()
            .all()
        )
        allowed = principal.agent.account_ids
        visible = [row for row in rows if not allowed or row.id in allowed]
        return {
            "accounts": [
                item.model_dump(mode="json") for item in await accounts_out(session, visible)
            ]
        }


@server.tool(description="List an account's folders (inbox, sent, etc.) with message counts.")
async def list_folders(account_id: str) -> dict[str, Any]:
    async with _agent_session("list_folders") as (session, principal):
        principal.require("mail.read")
        account = await principal.account(session, _uuid(account_id, "account_id"))
        return {
            "folders": [
                item.model_dump(mode="json") for item in await folders_out(session, account.id)
            ]
        }


@server.tool(
    description=(
        "Search messages in one account. `query` uses full-text search (subject, body, "
        "sender, recipients). Optional filters: folder_id, unread, flagged, has_attachments. "
        "Continue to the next page with `cursor`."
    )
)
async def search_messages(
    account_id: str,
    query: str | None = None,
    folder_id: str | None = None,
    unread: bool | None = None,
    flagged: bool | None = None,
    has_attachments: bool | None = None,
    limit: int = 25,
    cursor: str | None = None,
) -> dict[str, Any]:
    async with _agent_session("search_messages") as (session, principal):
        principal.require("mail.read")
        account = await principal.account(session, _uuid(account_id, "account_id"))
        bounded = max(1, min(limit, 100))
        try:
            rows, next_cursor = await list_messages(
                session,
                account.id,
                folder_id=_uuid(folder_id, "folder_id") if folder_id else None,
                q=query,
                unread=unread,
                flagged=flagged,
                has_attachments=has_attachments,
                limit=bounded,
                cursor=cursor,
            )
        except FolderNotFoundError as exc:
            raise ToolError(str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise ToolError("invalid cursor") from exc
        flags = await message_flags(session, [row.id for row in rows], None)
        return {
            "items": [message_out(row, flags.get(row.id)).model_dump(mode="json") for row in rows],
            "next_cursor": next_cursor,
        }


@server.tool(description="Fetch one message with its text/HTML body and attachment metadata.")
async def get_message(message_id: str, include_body: bool = True) -> dict[str, Any]:
    async with _agent_session("get_message") as (session, principal):
        principal.require("mail.read")
        message = await _message_of(session, principal, message_id)
        flags = await message_flags(session, [message.id])
        result: dict[str, Any] = {
            "message": message_out(message, flags.get(message.id)).model_dump(mode="json")
        }
        if include_body:
            body = await body_out(session, message)
            result["body"] = body.model_dump(mode="json")
        return result


@server.tool(description="Fetch every message in a conversation thread, ordered by time.")
async def get_thread(thread_id: str) -> dict[str, Any]:
    async with _agent_session("get_thread") as (session, principal):
        principal.require("mail.read")
        thread = await session.get(Thread, _uuid(thread_id, "thread_id"))
        if thread is None:
            raise ToolError("thread not found")
        await principal.account(session, thread.account_id)
        return {"thread": (await thread_out(session, thread)).model_dump(mode="json")}


@server.tool(description="Change a message's read status (`seen`) and/or flag (`flagged`).")
async def set_message_flags(
    message_id: str, seen: bool | None = None, flagged: bool | None = None
) -> dict[str, Any]:
    async with _agent_session("set_message_flags") as (session, principal):
        principal.require("mail.write")
        if seen is None and flagged is None:
            raise ToolError("no flags to change")
        message = await _message_of(session, principal, message_id)
        account = await principal.account(session, message.account_id)
        try:
            await actions.set_message_flags(
                session, account, message.id, seen=seen, flagged=flagged
            )
        except MessageNotFoundError as exc:
            raise ToolError(str(exc)) from exc
        await session.refresh(message)
        flags = await message_flags(session, [message.id])
        return {"message": message_out(message, flags.get(message.id)).model_dump(mode="json")}


@server.tool(description="Move a message to another folder in the same account.")
async def move_message(message_id: str, folder_id: str) -> dict[str, Any]:
    async with _agent_session("move_message") as (session, principal):
        principal.require("mail.write")
        message = await _message_of(session, principal, message_id)
        account = await principal.account(session, message.account_id)
        target = _uuid(folder_id, "folder_id")
        await principal.account(session, account.id)
        try:
            await actions.move_message(session, account, message.id, target)
        except (MessageNotFoundError, FolderTargetError) as exc:
            raise ToolError(str(exc)) from exc
        return {"status": "moved", "message_id": str(message.id), "folder_id": str(target)}


@server.tool(description="Delete a message (moved to Trash if the server supports it).")
async def delete_message(message_id: str) -> dict[str, Any]:
    async with _agent_session("delete_message") as (session, principal):
        principal.require("mail.delete")
        message = await _message_of(session, principal, message_id)
        account = await principal.account(session, message.account_id)
        try:
            await actions.delete_message(session, account, message.id)
        except MessageNotFoundError as exc:
            raise ToolError(str(exc)) from exc
        return {"status": "deleted", "message_id": str(message.id)}


@server.tool(
    description=(
        "Queue an outgoing message from an account. The sender is always that account's "
        "address. If the token requires approval, the message stops at `pending_approval` "
        "until a human approves it."
    )
)
async def send_message(
    account_id: str,
    to: list[str],
    subject: str = "",
    text: str | None = None,
    html: str | None = None,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    attachments: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    async with _agent_session("send_message") as (session, principal):
        principal.require("mail.send")
        account = await principal.account(session, _uuid(account_id, "account_id"))
        if not to:
            raise ToolError("at least one recipient is required (to)")
        if text is None and html is None:
            raise ToolError("empty message body: fill in `text` or `html`")
        try:
            await outbox_module.enforce_agent_send_limit(session, principal.agent)
        except outbox_module.SendLimitExceeded as exc:
            raise ToolError(str(exc)) from exc

        payload: dict[str, Any] = {
            "from": account.email_address,
            "to": to,
            "cc": cc or [],
            "bcc": bcc or [],
            "subject": subject,
            "text": text,
            "html": html,
            "in_reply_to": in_reply_to,
            "references": references or [],
            "attachments": [
                {
                    "filename": str(item.get("filename") or "attachment"),
                    "mime_type": str(item.get("mime_type") or "application/octet-stream"),
                    "content_b64": str(item.get("content_b64") or ""),
                }
                for item in (attachments or [])
            ],
        }
        requires_approval = bool(principal.agent.require_send_approval)
        row = await outbox_module.queue(
            session,
            account,
            payload=payload,
            created_by_user_id=principal.user.id,
            created_by_token_id=principal.agent.id,
            requires_approval=requires_approval,
        )
        if row.status == "queued":
            if _arq is None:
                raise ToolError("job queue is not ready; contact the administrator")
            await _arq.enqueue_job("deliver_outbox", str(account.id))
        return {
            "id": str(row.id),
            "status": row.status,
            "requires_approval": requires_approval,
            "to": to,
            "subject": subject,
        }


@server.tool(description="List an account's outgoing messages (outbox) with delivery status.")
async def list_outbox(account_id: str, limit: int = 20) -> dict[str, Any]:
    async with _agent_session("list_outbox") as (session, principal):
        principal.require("mail.read")
        account = await principal.account(session, _uuid(account_id, "account_id"))
        rows = (
            await session.execute(
                select(Outbox)
                .where(Outbox.account_id == account.id)
                .order_by(Outbox.created_at.desc())
                .limit(max(1, min(limit, 100)))
            )
        ).scalars()
        return {
            "items": [
                {
                    "id": str(row.id),
                    "status": row.status,
                    "subject": (row.payload or {}).get("subject"),
                    "to": (row.payload or {}).get("to"),
                    "attempts": row.attempts,
                    "last_error": row.last_error,
                    "created_at": row.created_at.isoformat(),
                    "sent_at": row.sent_at.isoformat() if row.sent_at else None,
                }
                for row in rows
            ]
        }


@server.tool(
    description=(
        "Fetch one attachment's content as base64. Attachments larger than `max_bytes` "
        "return only metadata; download the bytes via REST "
        "`/api/messages/{message_id}/attachments/{attachment_id}` with the same token."
    )
)
async def get_attachment(
    message_id: str, attachment_id: str, max_bytes: int = MAX_ATTACHMENT_BYTES
) -> dict[str, Any]:
    async with _agent_session("get_attachment") as (session, principal):
        principal.require("mail.read")
        message = await _message_of(session, principal, message_id)
        attachment = await session.get(Attachment, _uuid(attachment_id, "attachment_id"))
        if attachment is None or attachment.message_id != message.id:
            raise ToolError("attachment not found on this message")
        meta = {
            "message_id": str(message.id),
            "attachment_id": str(attachment.id),
            "filename": attachment.filename,
            "mime_type": attachment.mime_type,
            "size_bytes": attachment.size_bytes,
        }
        if (attachment.size_bytes or 0) > max_bytes:
            return {**meta, "content_b64": None, "truncated": True}
        try:
            data, mime_type = await load_attachment_bytes(session, message, attachment)
        except AttachmentUnavailableError as exc:
            raise ToolError(str(exc)) from exc
        if len(data) > max_bytes:
            return {**meta, "mime_type": mime_type, "content_b64": None, "truncated": True}
        return {
            **meta,
            "mime_type": mime_type,
            "content_b64": base64.b64encode(data).decode(),
            "truncated": False,
        }


def build_mcp_app() -> Any:
    """Starlette MCP app (Streamable HTTP) to mount on the API at `/api/mcp`."""
    return server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        # DNS-rebinding protection is disabled: requests already pass through the app's
        # TrustedHostMiddleware (LENOVMAIL_ALLOWED_HOSTS) and still require an agent token.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
