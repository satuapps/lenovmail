# Lenovmail — authored by satuapps
"""Folders, messages, attachments, write actions, and the send outbox.

An important rule for message listing: one message can live in multiple folders, so
folder filtering uses `mailbox_messages`, while message identity remains a single
`messages` row. Page cursors use a keyset (`internal_date`, `id`) so pagination doesn't
get slower on later pages the way it would with OFFSET.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from ... import blobs
from ...logging import get_logger
from ...models import (
    Account,
    Attachment,
    Message,
    Outbox,
    Thread,
)
from ...sync import actions
from ...sync import outbox as outbox_module
from ...sync.actions import MessageNotFoundError
from ...sync.attachments import AttachmentUnavailableError, load_attachment_bytes
from ...sync.outbox import SendLimitExceeded, enforce_agent_send_limit
from ...sync.queries import FolderNotFoundError
from ...sync.queries import list_messages as query_messages
from ..deps import AccountDep, ArqDep, PrincipalDep, SessionDep, require_read
from ..schemas import (
    BodyOut,
    FlagUpdate,
    FolderOut,
    MessageOut,
    MessagePage,
    MoveRequest,
    OutboxCreate,
    OutboxOut,
    ThreadOut,
)
from ..serializers import body_out, folders_out, message_flags, message_out, thread_out

log = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["mail"])


@router.get(
    "/accounts/{account_id}/folders",
    response_model=list[FolderOut],
    dependencies=[Depends(require_read)],
)
async def list_folders(account: AccountDep, session: SessionDep) -> list[FolderOut]:
    return await folders_out(session, account.id)


PageLimit = Annotated[int, Query(ge=1, le=200)]


@router.get(
    "/accounts/{account_id}/messages",
    response_model=MessagePage,
    dependencies=[Depends(require_read)],
)
async def list_messages(
    account: AccountDep,
    session: SessionDep,
    folder_id: uuid.UUID | None = None,
    q: str | None = None,
    unread: bool | None = None,
    flagged: bool | None = None,
    has_attachments: bool | None = None,
    thread_id: uuid.UUID | None = None,
    limit: PageLimit = 50,
    cursor: str | None = None,
) -> MessagePage:
    """List messages with folder/status filters and full-text search."""
    try:
        rows, next_cursor = await query_messages(
            session,
            account.id,
            folder_id=folder_id,
            q=q,
            unread=unread,
            flagged=flagged,
            has_attachments=has_attachments,
            thread_id=thread_id,
            limit=limit,
            cursor=cursor,
        )
    except FolderNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid cursor") from exc

    flags = await message_flags(session, [row.id for row in rows], folder_id)
    items = [message_out(row, flags.get(row.id)) for row in rows]
    return MessagePage(items=items, next_cursor=next_cursor)


async def _message_dep(
    message_id: uuid.UUID, principal: PrincipalDep, session: SessionDep
) -> Message:
    row = await session.get(Message, message_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="message not found")
    account = await session.get(Account, row.account_id)
    if account is None or account.owner_id != principal.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="message not found")
    principal.ensure_account(row.account_id)
    return row


MessageDep = Annotated[Message, Depends(_message_dep)]


async def _account_of(session: SessionDep, message: Message) -> Account:
    account = await session.get(Account, message.account_id)
    if account is None:  # pragma: no cover - guarded by `_message_dep`
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="account not found")
    return account


@router.get(
    "/messages/{message_id}", response_model=MessageOut, dependencies=[Depends(require_read)]
)
async def get_message(message: MessageDep, session: SessionDep) -> MessageOut:
    flags = await message_flags(session, [message.id])
    return message_out(message, flags.get(message.id))


@router.get(
    "/messages/{message_id}/body", response_model=BodyOut, dependencies=[Depends(require_read)]
)
async def get_message_body(message: MessageDep, session: SessionDep) -> BodyOut:
    return await body_out(session, message)


@router.get(
    "/messages/{message_id}/raw",
    response_class=PlainTextResponse,
    dependencies=[Depends(require_read)],
)
async def get_message_raw(message: MessageDep, session: SessionDep) -> Response:
    """Raw MIME from the blob store (for .eml download and auditing)."""
    if message.blob_sha256 is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="raw content is not stored")
    raw = await blobs.get(message.blob_sha256)
    if raw is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="blob is missing")
    filename = (message.subject or "message")[:80].replace('"', "") or "message"
    return PlainTextResponse(
        content=raw.decode("utf-8", "replace"),
        media_type="message/rfc822",
        headers={"Content-Disposition": f'attachment; filename="{filename}.eml"'},
    )


@router.get(
    "/messages/{message_id}/attachments/{attachment_id}", dependencies=[Depends(require_read)]
)
async def get_attachment(
    message: MessageDep, attachment_id: uuid.UUID, session: SessionDep
) -> Response:
    """Download an attachment.

    Attachment bytes aren't stored separately: for messages with a MIME blob, the part
    is extracted from that blob by `part_path`. Graph messages whose body is fetched
    over JSON (no blob) pull their content directly from Graph.
    """
    attachment = await session.get(Attachment, attachment_id)
    if attachment is None or attachment.message_id != message.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="attachment not found")

    try:
        data, mime_type = await load_attachment_bytes(session, message, attachment)
    except AttachmentUnavailableError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - the Graph error is forwarded as-is
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, detail=f"failed to fetch attachment from Graph: {exc}"
        ) from exc

    filename = (attachment.filename or "attachment").replace('"', "")
    return Response(
        content=data,
        media_type=mime_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(data)),
        },
    )


@router.post("/messages/{message_id}/flags", response_model=MessageOut)
async def update_flags(
    message: MessageDep, body: FlagUpdate, principal: PrincipalDep, session: SessionDep
) -> MessageOut:
    principal.require("mail.write")
    if body.seen is None and body.flagged is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="no flags were changed")
    account = await _account_of(session, message)
    try:
        await actions.set_message_flags(
            session, account, message.id, seen=body.seen, flagged=body.flagged
        )
    except MessageNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    await session.refresh(message)
    flags = await message_flags(session, [message.id])
    return message_out(message, flags.get(message.id))


@router.post("/messages/{message_id}/move", status_code=status.HTTP_202_ACCEPTED)
async def move_message(
    message: MessageDep, body: MoveRequest, principal: PrincipalDep, session: SessionDep
) -> dict[str, str]:
    principal.require("mail.write")
    account = await _account_of(session, message)
    try:
        await actions.move_message(session, account, message.id, body.folder_id)
    except (MessageNotFoundError, actions.FolderTargetError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"status": "moved"}


@router.delete("/messages/{message_id}", status_code=status.HTTP_202_ACCEPTED)
async def delete_message(
    message: MessageDep, principal: PrincipalDep, session: SessionDep
) -> dict[str, str]:
    principal.require("mail.delete")
    account = await _account_of(session, message)
    try:
        await actions.delete_message(session, account, message.id)
    except MessageNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return {"status": "deleted"}


@router.get("/threads/{thread_id}", response_model=ThreadOut, dependencies=[Depends(require_read)])
async def get_thread(
    thread_id: uuid.UUID, principal: PrincipalDep, session: SessionDep
) -> ThreadOut:
    thread = await session.get(Thread, thread_id)
    if thread is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="thread not found")
    account = await session.get(Account, thread.account_id)
    if account is None or account.owner_id != principal.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="thread not found")
    return await thread_out(session, thread)


# --- outbox ---------------------------------------------------------------------


@router.post(
    "/accounts/{account_id}/outbox",
    response_model=OutboxOut,
    status_code=status.HTTP_201_CREATED,
)
async def queue_outbox(
    account: AccountDep,
    body: OutboxCreate,
    principal: PrincipalDep,
    session: SessionDep,
    arq: ArqDep,
) -> OutboxOut:
    """Queue an outgoing message. Agent tokens can be required to go through manual
    approval."""
    principal.require("mail.send")
    requires_approval = body.requires_approval
    if principal.agent is not None:
        # The send limit applies to every agent token, not only ones that need approval.
        try:
            await enforce_agent_send_limit(session, principal.agent)
        except SendLimitExceeded as exc:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc)) from exc
        requires_approval = requires_approval or principal.agent.require_send_approval
    row = await outbox_module.queue(
        session,
        account,
        payload=body.payload.model_dump(by_alias=True),
        created_by_user_id=principal.user_id,
        created_by_token_id=principal.agent.id if principal.agent else None,
        requires_approval=requires_approval,
    )
    if row.status == "queued":
        await arq.enqueue_job("deliver_outbox", str(account.id))
    return OutboxOut.model_validate(row)


@router.get(
    "/accounts/{account_id}/outbox",
    response_model=list[OutboxOut],
    dependencies=[Depends(require_read)],
)
async def list_outbox(
    account: AccountDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[OutboxOut]:
    rows = (
        await session.execute(
            select(Outbox)
            .where(Outbox.account_id == account.id)
            .order_by(Outbox.created_at.desc())
            .limit(limit)
        )
    ).scalars()
    return [OutboxOut.model_validate(row) for row in rows]


@router.post("/outbox/{outbox_id}/approve", response_model=OutboxOut)
async def approve_outbox(
    outbox_id: uuid.UUID, principal: PrincipalDep, session: SessionDep, arq: ArqDep
) -> OutboxOut:
    principal.require("mail.send")
    row = await session.get(Outbox, outbox_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="outbox message not found")
    account = await session.get(Account, row.account_id)
    if account is None or account.owner_id != principal.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="outbox message not found")

    row = await outbox_module.approve(session, row)
    await arq.enqueue_job("deliver_outbox", str(account.id))
    return OutboxOut.model_validate(row)
