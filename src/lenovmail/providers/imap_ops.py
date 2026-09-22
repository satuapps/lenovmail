# Lenovmail — authored by satuapps (satuapps.com)
"""High-level operations on top of `ImapConn` (folders, headers, body, flags, move, append)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..logging import get_logger
from .imap_pool import ImapConn

log = get_logger(__name__)

HEADER_FETCH_DATA = [
    "UID",
    "FLAGS",
    "INTERNALDATE",
    "RFC822.SIZE",
    "ENVELOPE",
]
RAW_FETCH_DATA = ["BODY.PEEK[]"]

# Headers sufficient to display a message before the body has been fetched.
HEADER_ONLY_FETCH_DATA = ["BODY.PEEK[HEADER]"]

FLAG_SEEN = "\\Seen"
FLAG_FLAGGED = "\\Flagged"
FLAG_ANSWERED = "\\Answered"
FLAG_DRAFT = "\\Draft"
FLAG_DELETED = "\\Deleted"

# SPECIAL-USE (RFC 6154) -> internal role.
SPECIAL_USE_ROLES: dict[str, str] = {
    "\\INBOX": "inbox",
    "\\SENT": "sent",
    "\\DRAFTS": "drafts",
    "\\TRASH": "trash",
    "\\JUNK": "junk",
    "\\ARCHIVE": "archive",
    "\\ALL": "archive",
}

# Folder-name heuristics for servers without SPECIAL-USE.
NAME_ROLES: dict[str, str] = {
    "inbox": "inbox",
    "sent": "sent",
    "sent items": "sent",
    "sent messages": "sent",
    "sent mail": "sent",
    "terkirim": "sent",
    "drafts": "drafts",
    "draft": "drafts",
    "konsep": "drafts",
    "trash": "trash",
    "deleted items": "trash",
    "deleted messages": "trash",
    "sampah": "trash",
    "junk": "junk",
    "junk e-mail": "junk",
    "spam": "junk",
    "archive": "archive",
    "archives": "archive",
    "arsip": "archive",
    "all mail": "archive",
}


@dataclass(frozen=True, slots=True)
class SelectInfo:
    uidvalidity: int | None
    uidnext: int | None
    highestmodseq: int | None


@dataclass(frozen=True, slots=True)
class FolderInfo:
    name: str
    delimiter: str
    flags: tuple[str, ...]
    role: str


def role_for_folder(name: str, flags: tuple[str, ...]) -> str:
    """Determine the folder role: SPECIAL-USE first, then name heuristics (case-insensitive)."""
    for flag in flags:
        mapped = SPECIAL_USE_ROLES.get(flag.upper())
        if mapped is not None:
            return mapped
    normalised = name.strip().lower().replace("\\", "/").split("/")[-1].strip()
    return NAME_ROLES.get(normalised, "other")


def _decode_flag(flag: bytes | str) -> str:
    return flag.decode("ascii", "replace") if isinstance(flag, bytes) else str(flag)


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_modseq(data: dict) -> int | None:
    """MODSEQ from a FETCH entry.

    CONDSTORE returns `MODSEQ` as a tuple `(modseq,)` on some servers and
    as an int on others.
    """
    raw = data.get(b"MODSEQ")
    if isinstance(raw, tuple | list):
        raw = raw[0] if raw else None
    return _int_or_none(raw)


async def list_folders(conn: ImapConn) -> list[FolderInfo]:
    raw = await conn.list_folders()
    folders: list[FolderInfo] = []
    for flags, delimiter, name in raw:
        decoded_flags = tuple(_decode_flag(f) for f in flags)
        # `imapclient` returns the delimiter as bytes; all consumers (role, folder
        # hierarchy) treat it as text, so it is normalized here.
        decoded_delimiter = _decode_flag(delimiter).strip() if delimiter else "/"
        folders.append(
            FolderInfo(
                name=name,
                delimiter=decoded_delimiter or "/",
                flags=decoded_flags,
                role=role_for_folder(name, decoded_flags),
            )
        )
    return folders


async def select(conn: ImapConn, folder: str, readonly: bool = False) -> SelectInfo:
    info = await conn.select_folder(folder, readonly=readonly)
    return SelectInfo(
        uidvalidity=_int_or_none(info.get(b"UIDVALIDITY")),
        uidnext=_int_or_none(info.get(b"UIDNEXT")),
        highestmodseq=_int_or_none(info.get(b"HIGHESTMODSEQ")),
    )


async def search_uids(conn: ImapConn, criteria: Any) -> list[int]:
    return sorted(await conn.search(criteria))


async def search_all_uids(conn: ImapConn) -> list[int]:
    return await search_uids(conn, ["ALL"])


async def search_uids_since(conn: ImapConn, first_uid: int) -> list[int]:
    """New UIDs since `first_uid` (inclusive).

    `UID n:*` always returns the last message when nothing matches, so the result
    must be re-filtered against `first_uid`.
    """
    uids = await search_uids(conn, ["UID", f"{first_uid}:*"])
    return [uid for uid in uids if uid >= first_uid]


async def fetch_headers(conn: ImapConn, uids: list[int]) -> dict[int, dict]:
    """Envelope + flags + size for a set of UIDs (without body)."""
    if not uids:
        return {}
    return await conn.fetch(uids, HEADER_FETCH_DATA)


async def fetch_raw(conn: ImapConn, uids: list[int]) -> dict[int, bytes]:
    """Full raw MIME per UID. Messages that don't return a body are skipped."""
    if not uids:
        return {}
    response = await conn.fetch(uids, RAW_FETCH_DATA)
    out: dict[int, bytes] = {}
    for uid, data in response.items():
        raw = data.get(b"BODY[]")
        if raw:
            out[uid] = raw
    return out


async def fetch_header_only(conn: ImapConn, uid: int) -> bytes | None:
    response = await conn.fetch([uid], HEADER_ONLY_FETCH_DATA)
    data = response.get(uid) or {}
    return data.get(b"BODY[HEADER]")


# Chunk size for FETCH with an explicit UID list. `imapclient.fetch` rejects
# ranges like "1:*" (it requires a list of ints), so long UID lists are chunked
# to keep the IMAP command line from ballooning.
UID_CHUNK = 5000


def _uid_of(key: Any, data: dict) -> int | None:
    """UID from a FETCH response entry.

    The dict key is already the UID when `use_uid=True`; the `UID` item inside the data
    may not be sent by the server, so the key is used as the fallback (not the other way around).
    """
    uid = _int_or_none(data.get(b"UID"))
    return uid if uid is not None else _int_or_none(key)


async def fetch_flags_changed_since(
    conn: ImapConn, modseq: int, uids: list[int]
) -> dict[int, list[str]]:
    """UIDs + flags changed since `modseq` (requires CONDSTORE, RFC 7162).

    The caller must supply the folder's UID list (SEARCH result); the CHANGEDSINCE
    modifier filters so only messages that actually changed are sent by the server.
    """
    out: dict[int, list[str]] = {}
    for start in range(0, len(uids), UID_CHUNK):
        chunk = uids[start : start + UID_CHUNK]
        response = await conn.fetch(chunk, ["UID", "FLAGS"], modifiers=[f"CHANGEDSINCE {modseq}"])
        for key, data in response.items():
            uid = _uid_of(key, data)
            if uid is None:
                continue
            out[uid] = [_decode_flag(f) for f in (data.get(b"FLAGS") or [])]
    return out


async def fetch_all_flags(conn: ImapConn, uids: list[int]) -> dict[int, list[str]]:
    """Flags for all given UIDs, chunked by `UID_CHUNK`."""
    out: dict[int, list[str]] = {}
    for start in range(0, len(uids), UID_CHUNK):
        chunk = uids[start : start + UID_CHUNK]
        response = await conn.fetch(chunk, ["UID", "FLAGS"])
        for key, data in response.items():
            uid = _uid_of(key, data)
            if uid is None:
                continue
            out[uid] = [_decode_flag(f) for f in (data.get(b"FLAGS") or [])]
    return out


async def set_flags(
    conn: ImapConn,
    uid: int,
    *,
    add: tuple[str, ...] = (),
    remove: tuple[str, ...] = (),
) -> None:
    if add:
        await conn.add_flags([uid], list(add), silent=True)
    if remove:
        await conn.remove_flags([uid], list(remove), silent=True)


async def move(conn: ImapConn, uid: int, destination: str) -> None:
    """Move a message. Without the MOVE capability: COPY + \\Deleted + EXPUNGE."""
    if conn.caps.get("move"):
        await conn.move([uid], destination)
        return

    await conn.copy([uid], destination)
    await conn.add_flags([uid], [FLAG_DELETED], silent=True)
    if conn.caps.get("uidplus"):
        await conn.uid_expunge([uid])
    else:
        # Servers without UIDPLUS can only expunge all \Deleted-flagged messages.
        await conn.expunge()


async def append(conn: ImapConn, folder: str, raw: bytes, seen: bool = True) -> None:
    flags = (FLAG_SEEN,) if seen else ()
    await conn.append(folder, raw, flags=flags)


async def delete_message(conn: ImapConn, uid: int) -> None:
    """Permanently delete: flag \\Deleted then expunge (UIDPLUS when available)."""
    await conn.add_flags([uid], [FLAG_DELETED], silent=True)
    if conn.caps.get("uidplus"):
        await conn.uid_expunge([uid])
    else:
        await conn.expunge()
