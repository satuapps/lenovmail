# Lenovmail — authored by satuapps (satuapps.com)
"""Normalize raw MIME into a message record ready for storage.

Input is always the full MIME bytes (from IMAP `BODY.PEEK[]` or Graph `/$value`). The
output is a `ParsedMessage` dataclass mapped directly onto the `messages`,
`message_bodies`, `attachments`, and `message_refs` tables.

The parser never raises for malformed messages: broken headers or an unknown charset
are replaced (`errors='replace'`) and the message is still stored.
"""

from __future__ import annotations

import hashlib
import html as html_mod
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.message import Message
from email.parser import BytesParser
from email.policy import default as default_policy
from email.utils import getaddresses, parsedate_to_datetime

import nh3

from ..logging import get_logger

log = get_logger(__name__)

SNIPPET_CHARS = 200

# Reply/forward prefixes stripped when building `subject_norm`. Localized abbreviations as
# sent by mainstream clients: German (aw), Nordic (sv, vs), Dutch (antw), Indonesian (bls).
# Keep this list in sync with `stripSubjectPrefix` in web/src/features/mail/mailUtils.ts.
_REPLY_PREFIX = re.compile(
    r"^\s*((re|fw|fwd|aw|sv|vs|antw|bls|balas)\s*(\[\d+\])?\s*:\s*)+",
    re.IGNORECASE,
)
_WHITESPACE = re.compile(r"\s+")
_BLOCK_TAGS = re.compile(r"(?i)<\s*(br|/p|/div|/tr|/li|/h[1-6]|/blockquote|/table)\s*/?\s*>")

# Headers stored in `messages.headers` (keys are always lowercase).
STORED_HEADERS = (
    "message-id",
    "in-reply-to",
    "references",
    "list-id",
    "list-unsubscribe",
    "return-path",
    "received-spf",
    "authentication-results",
    "x-mailer",
    "content-type",
)

# Disposition values treated as attachments even without a filename.
ATTACHMENT_DISPOSITIONS = {"attachment", "inline"}


@dataclass(frozen=True, slots=True)
class ParsedAttachment:
    part_path: str
    filename: str | None
    mime_type: str | None
    size_bytes: int | None
    content_id: str | None
    is_inline: bool


@dataclass(slots=True)
class ParsedMessage:
    dedup_hash: bytes
    # `None` for messages stored from headers only (too large, or Graph refused `$value`).
    size_bytes: int | None
    rfc822_message_id: str | None = None
    subject: str | None = None
    subject_norm: str | None = None
    from_name: str | None = None
    from_addr: str | None = None
    to_addrs: list[dict[str, str]] = field(default_factory=list)
    cc_addrs: list[dict[str, str]] = field(default_factory=list)
    bcc_addrs: list[dict[str, str]] = field(default_factory=list)
    reply_to: list[dict[str, str]] = field(default_factory=list)
    sent_date: datetime | None = None
    has_attachments: bool = False
    snippet: str | None = None
    body_text: str | None = None
    body_html: str | None = None
    attachments: list[ParsedAttachment] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    in_reply_to: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


def normalize_subject(subject: str | None) -> str | None:
    """Strip reply prefixes and normalize whitespace; result is lowercased for thread matching."""
    if not subject:
        return None
    stripped = _REPLY_PREFIX.sub("", subject)
    collapsed = _WHITESPACE.sub(" ", stripped).strip()
    return collapsed.lower() or None


def sanitize_html(raw_html: str) -> str:
    """Sanitize HTML for display (used by the GUI, served from the DB)."""
    return nh3.clean(raw_html, link_rel="noopener noreferrer nofollow")


def html_to_text(raw_html: str) -> str:
    """Convert HTML to plain text; used as a body fallback and for snippets."""
    with_newlines = _BLOCK_TAGS.sub("\n", raw_html)
    stripped = nh3.clean(with_newlines, tags=set())
    return html_mod.unescape(stripped)


def _collapse(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def _decode_part(part: Message) -> str | None:
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        log.warning("mime_part_decode_failed", exc_info=True, content_type=part.get_content_type())
        return None
    if not isinstance(payload, bytes):
        # Non-multipart part without decoded payload: use raw text if available.
        raw = part.get_payload()
        return raw if isinstance(raw, str) else None
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        # Unknown charset (common in spam): read as utf-8 with replacement.
        log.info("unknown_charset", charset=charset)
        return payload.decode("utf-8", errors="replace")


def extract_part(raw: bytes, part_path: str) -> tuple[bytes, str | None] | None:
    """Extract one MIME part's content by its `part_path` numbering (`1`, `1.2`, ...).

    Used for attachment downloads: attachment bytes aren't stored separately — they're
    extracted from the message's full MIME blob, keeping one copy per message instead of two.
    """
    for path, part in _walk_parts(BytesParser(policy=default_policy).parsebytes(raw)):
        if path != part_path:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            text = part.get_payload()
            if not isinstance(text, str):
                return None
            return text.encode(
                part.get_content_charset() or "utf-8", "replace"
            ), part.get_content_type()
        return payload, part.get_content_type()
    return None


def _content_id(part: Message) -> str | None:
    raw = part.get("Content-ID")
    if not raw:
        return None
    return raw.strip().strip("<>") or None


def _is_attachment(part: Message) -> bool:
    disposition = (part.get_content_disposition() or "").lower()
    if disposition == "attachment":
        return True
    return part.get_filename() is not None


def _walk_parts(part: Message, prefix: str = ""):
    """Walk MIME parts using IMAP-style part numbering (`1`, `1.2`, `2.1`)."""
    if part.is_multipart():
        for index, sub in enumerate(part.get_payload(), start=1):
            child_prefix = f"{prefix}.{index}" if prefix else str(index)
            yield from _walk_parts(sub, child_prefix)
        return
    yield prefix or "1", part


def _addresses(message: Message, header: str) -> list[dict[str, str]]:
    values = message.get_all(header) or []
    out: list[dict[str, str]] = []
    for value in values:
        for display, addr in getaddresses([str(value)]):
            if addr and addr.strip():
                out.append({"name": display or "", "addr": addr.strip().lower()})
    return out


def _parse_date(message: Message, header: str) -> datetime | None:
    raw = message.get(header)
    if not raw:
        return None
    try:
        return parsedate_to_datetime(str(raw))
    except (TypeError, ValueError):
        log.info("date_header_unparsable", header=header)
        return None


def _header_str(message: Message, name: str) -> str | None:
    """Header value as a str, or None if the header is absent.

    `str(message.get(name))` is dangerous: a missing header becomes the string `'None'`,
    which then gets used as the Message-ID and **collapses every message without a
    Message-ID into one dedup_hash**. Always go through this helper.
    """
    value = message.get(name)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _dedup_hash(message_id: str | None, raw: bytes) -> bytes:
    if message_id:
        return hashlib.sha256(message_id.encode("utf-8", "replace")).digest()
    return hashlib.sha256(raw).digest()


def parse_message(raw: bytes) -> ParsedMessage:
    """Parse MIME bytes into a `ParsedMessage`. Never raises for malformed messages."""
    message = BytesParser(policy=default_policy).parsebytes(raw)

    part_paths = list(_walk_parts(message))
    body_text: str | None = None
    body_html_raw: str | None = None
    attachments: list[ParsedAttachment] = []

    for part_path, part in part_paths:
        content_type = (part.get_content_type() or "").lower()
        content_id = _content_id(part)
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        is_attachment = _is_attachment(part)

        if is_attachment or content_type.startswith(("image/", "application/", "audio/", "video/")):
            payload_size: int | None = None
            if content_type != "message/rfc822":
                decoded = part.get_payload(decode=True)
                payload_size = len(decoded) if decoded is not None else None
            attachments.append(
                ParsedAttachment(
                    part_path=part_path,
                    filename=filename,
                    mime_type=content_type or None,
                    size_bytes=payload_size,
                    content_id=content_id,
                    is_inline=disposition == "inline" or content_id is not None,
                )
            )
            continue

        if content_type == "text/plain" and body_text is None:
            body_text = _decode_part(part)
        elif content_type == "text/html" and body_html_raw is None:
            body_html_raw = _decode_part(part)

    if body_text is None and body_html_raw is not None:
        body_text = html_to_text(body_html_raw)

    # Empty body is stored as NULL, not '' — keeps body_state/snippet consistent
    # and avoids empty search entries.
    if body_text is not None and not body_text.strip():
        body_text = None
    if body_html_raw is not None and not body_html_raw.strip():
        body_html_raw = None

    body_html = sanitize_html(body_html_raw) if body_html_raw else None

    subject_raw = message.get("Subject")
    subject = _collapse(str(subject_raw)) if subject_raw else None

    message_id = _header_str(message, "Message-ID")
    in_reply_to = _header_str(message, "In-Reply-To")

    refs: list[str] = []
    for raw_refs in message.get_all("References") or []:
        refs.extend(token for token in str(raw_refs).split() if token)
    if in_reply_to and in_reply_to not in refs:
        refs.append(in_reply_to)

    headers: dict[str, str] = {}
    for name in STORED_HEADERS:
        value = message.get(name)
        if value:
            headers[name] = str(value)[:4000]

    snippet_source = body_text or ""
    snippet = _collapse(snippet_source)[:SNIPPET_CHARS] or None

    reply_to = _addresses(message, "Reply-To")
    from_addrs = _addresses(message, "From") or [{"name": "", "addr": ""}]
    return ParsedMessage(
        dedup_hash=_dedup_hash(message_id, raw),
        size_bytes=len(raw),
        rfc822_message_id=message_id,
        subject=subject,
        subject_norm=normalize_subject(subject),
        from_name=from_addrs[0]["name"] or None,
        from_addr=from_addrs[0]["addr"] or None,
        to_addrs=_addresses(message, "To"),
        cc_addrs=_addresses(message, "Cc"),
        bcc_addrs=_addresses(message, "Bcc"),
        reply_to=reply_to,
        sent_date=_parse_date(message, "Date"),
        has_attachments=bool(attachments),
        snippet=snippet,
        body_text=body_text,
        body_html=body_html,
        attachments=attachments,
        refs=refs,
        in_reply_to=in_reply_to,
        headers=headers,
    )


def search_tsvector_input(
    subject: str | None, from_name: str | None, from_addr: str | None, body_text: str | None
) -> str:
    """Text fed into `to_tsvector('simple', ...)` for `message_search.tsv`."""
    return " ".join(part for part in (subject, from_name, from_addr, body_text) if part)
