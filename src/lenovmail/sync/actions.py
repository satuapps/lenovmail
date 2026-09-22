# Lenovmail — authored by satuapps
"""Write actions on messages: change flags, move folders, and delete.

Patterns used:

* **Flag** — write locally first (the GUI stays responsive immediately), then push to the
  server. The final flag value is known for certain, so no resync is needed.
* **Move/delete** — server first (that is where the truth lives), then sync the source/
  destination folders so local UID/delta state follows along. If the follow-up sync
  fails, the action is still considered successful: the change already happened on the
  server, and the next periodic sync cycle will tidy up the local state.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..logging import get_logger
from ..models import Account, Folder, MailboxMessage
from ..providers import imap_ops as ops
from ..providers.accounts import get_account_pool, graph_client_for, save_graph_tokens
from . import store
from .graph_sync import sync_folder as graph_sync_folder
from .imap_sync import sync_folder as imap_sync_folder

log = get_logger(__name__)

IMAP_SEEN = "\\Seen"
IMAP_FLAGGED = "\\Flagged"


class MessageNotFoundError(Exception):
    """Message has no placement that can be changed."""


class FolderTargetError(Exception):
    """Destination folder does not belong to the same account."""


@dataclass(slots=True)
class Placement:
    folder_id: uuid.UUID
    folder_path: str
    folder_remote_id: str
    remote_uid: int | None
    remote_item_id: str | None


async def placements_for_message(
    session: AsyncSession, account_id: uuid.UUID, message_id: uuid.UUID
) -> list[Placement]:
    rows = (
        await session.execute(
            select(
                Folder.id,
                Folder.path,
                Folder.remote_id,
                MailboxMessage.remote_uid,
                MailboxMessage.remote_item_id,
            )
            .join(MailboxMessage, MailboxMessage.folder_id == Folder.id)
            .where(MailboxMessage.message_id == message_id, Folder.account_id == account_id)
        )
    ).all()
    return [
        Placement(
            folder_id=row[0],
            folder_path=row[1] or "",
            folder_remote_id=row[2],
            remote_uid=row[3],
            remote_item_id=row[4],
        )
        for row in rows
    ]


async def set_message_flags(
    session: AsyncSession,
    account: Account,
    message_id: uuid.UUID,
    *,
    seen: bool | None = None,
    flagged: bool | None = None,
) -> int:
    """Apply flags to every placement of the message; return the count of affected placements."""
    targets = await placements_for_message(session, account.id, message_id)
    if not targets:
        raise MessageNotFoundError("message not found in this account")

    local: dict[str, bool] = {}
    if seen is not None:
        local["flag_seen"] = seen
    if flagged is not None:
        local["flag_flagged"] = flagged
    updated = await store.set_flag_columns(session, message_id, local)
    await session.commit()

    if account.provider == "imap":
        pool = await get_account_pool(session, account)
        try:
            for placement in targets:
                if placement.remote_uid is None:
                    continue
                add = _imap_flags(seen, flagged, wanted=True)
                remove = _imap_flags(seen, flagged, wanted=False)
                async with pool.connection() as conn:
                    await ops.select(conn, placement.folder_path, readonly=False)
                    await ops.set_flags(
                        conn, placement.remote_uid, add=tuple(add), remove=tuple(remove)
                    )
        finally:
            await pool.close()
    else:
        client = await graph_client_for(session, account)
        try:
            payload: dict[str, object] = {}
            if seen is not None:
                payload["isRead"] = seen
            if flagged is not None:
                payload["flag"] = {"flagStatus": "flagged" if flagged else "notFlagged"}
            for placement in targets:
                if placement.remote_item_id:
                    await client.patch_message(placement.remote_item_id, payload)
            await save_graph_tokens(session, account.id, client)
        finally:
            await client.aclose()
    return updated


def _imap_flags(seen: bool | None, flagged: bool | None, *, wanted: bool) -> list[str]:
    flags: list[str] = []
    if seen is not None and seen is wanted:
        flags.append(IMAP_SEEN)
    if flagged is not None and flagged is wanted:
        flags.append(IMAP_FLAGGED)
    return flags


async def _drop_local_placement(
    session: AsyncSession, account: Account, placement: Placement
) -> None:
    """Drop the local placement that no longer exists on the server after a successful action.

    Without this, a message already moved/deleted on the server still shows up in the list
    until the flag-refresh cycle happens to run (every `FLAG_REFRESH_EVERY` cycles).
    """
    if placement.remote_uid is not None:
        await store.remove_placements_by_uid(
            session, account.id, placement.folder_id, [placement.remote_uid]
        )
    elif placement.remote_item_id is not None:
        await store.remove_placements(
            session, account.id, placement.folder_id, [placement.remote_item_id]
        )


async def move_message(
    session: AsyncSession,
    account: Account,
    message_id: uuid.UUID,
    destination_folder_id: uuid.UUID,
) -> None:
    """Move a message to another folder in the same account, then sync the related folders."""
    destination = await session.get(Folder, destination_folder_id)
    if destination is None or destination.account_id != account.id:
        raise FolderTargetError("destination folder does not belong to this account")
    sources = await placements_for_message(session, account.id, message_id)
    if not sources:
        raise MessageNotFoundError("message not found in this account")

    if account.provider == "imap":
        pool = await get_account_pool(session, account)
        try:
            for placement in sources:
                if placement.remote_uid is None or placement.folder_id == destination.id:
                    continue
                async with pool.connection() as conn:
                    await ops.select(conn, placement.folder_path, readonly=False)
                    await ops.move(conn, placement.remote_uid, destination.path or destination.name)
                await _drop_local_placement(session, account, placement)
            await session.commit()
        finally:
            await pool.close()
    else:
        client = await graph_client_for(session, account)
        try:
            for placement in sources:
                if placement.remote_item_id and placement.folder_id != destination.id:
                    await client.move_message(placement.remote_item_id, destination.remote_id)
                    await _drop_local_placement(session, account, placement)
            await session.commit()
            await save_graph_tokens(session, account.id, client)
        finally:
            await client.aclose()

    await _resync(
        session, account, [placement.folder_id for placement in sources] + [destination.id]
    )


async def delete_message(session: AsyncSession, account: Account, message_id: uuid.UUID) -> None:
    """Delete a message: IMAP `\\Deleted` + expunge, Graph move to Deleted Items."""
    sources = await placements_for_message(session, account.id, message_id)
    if not sources:
        raise MessageNotFoundError("message not found in this account")

    if account.provider == "imap":
        pool = await get_account_pool(session, account)
        try:
            for placement in sources:
                if placement.remote_uid is None:
                    continue
                async with pool.connection() as conn:
                    await ops.select(conn, placement.folder_path, readonly=False)
                    await ops.delete_message(conn, placement.remote_uid)
                await _drop_local_placement(session, account, placement)
            await session.commit()
        finally:
            await pool.close()
    else:
        client = await graph_client_for(session, account)
        try:
            for placement in sources:
                if placement.remote_item_id:
                    await client.delete_message(placement.remote_item_id)
                    await _drop_local_placement(session, account, placement)
            await session.commit()
            await save_graph_tokens(session, account.id, client)
        finally:
            await client.aclose()

    await _resync(session, account, [placement.folder_id for placement in sources])


async def _resync(session: AsyncSession, account: Account, folder_ids: Iterable[uuid.UUID]) -> None:
    """Sync the affected folders; failures are logged, not treated as failing the
    action that already happened."""
    for folder_id in dict.fromkeys(folder_ids):
        folder = await session.get(Folder, folder_id)
        if folder is None:
            continue
        try:
            if account.provider == "imap":
                pool = await get_account_pool(session, account)
                try:
                    async with pool.connection() as conn:
                        await imap_sync_folder(session, account, folder, conn)
                finally:
                    await pool.close()
            else:
                client = await graph_client_for(session, account)
                try:
                    await graph_sync_folder(session, account, folder, client)
                    await save_graph_tokens(session, account.id, client)
                finally:
                    await client.aclose()
        except Exception:
            log.warning("post_action_resync_failed", folder_id=str(folder_id), exc_info=True)
