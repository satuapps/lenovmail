# Lenovmail — authored by satuapps
"""Mail data models: folders, threads, messages, bodies, search, attachments, outbox, sync runs."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDPk

ROLE_VALUES = "'inbox','sent','drafts','trash','junk','archive','other'"
SYNC_STATE_VALUES = "'idle','syncing','error'"
BODY_STATE_VALUES = "'none','partial','full'"
OUTBOX_STATUS_VALUES = "'pending_approval','queued','sending','sent','failed'"


class Folder(UUIDPk, Base):
    __tablename__ = "folders"
    __table_args__ = (
        CheckConstraint(f"role in ({ROLE_VALUES})", name="folders_role_check"),
        CheckConstraint(f"sync_state in ({SYNC_STATE_VALUES})", name="folders_sync_state_check"),
        UniqueConstraint("account_id", "remote_id", name="folders_account_remote_key"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    remote_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str | None] = mapped_column(Text)
    delimiter: Mapped[str | None] = mapped_column(Text)

    # Graph uses well-known names; IMAP uses the SPECIAL-USE flag or a name heuristic.
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default="other")
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("folders.id", ondelete="SET NULL")
    )

    # Per-folder IMAP cursor.
    uidvalidity: Mapped[int | None] = mapped_column(BigInteger)
    uidnext: Mapped[int | None] = mapped_column(BigInteger)
    highestmodseq: Mapped[int | None] = mapped_column(BigInteger)

    # Per-folder Graph cursor (deltaLink holds the long-lived sync token).
    delta_link_enc: Mapped[bytes | None] = mapped_column(LargeBinary)

    sync_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="idle")
    sync_error: Mapped[str | None] = mapped_column(Text)
    sync_pass: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class Thread(UUIDPk, Base):
    __tablename__ = "threads"
    __table_args__ = (
        Index("threads_account_last_idx", "account_id", text("last_message_at DESC")),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    subject_norm: Mapped[str | None] = mapped_column(Text)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class Message(UUIDPk, Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(f"body_state in ({BODY_STATE_VALUES})", name="messages_body_state_check"),
        UniqueConstraint("account_id", "dedup_hash", name="messages_account_dedup_key"),
        Index(
            "messages_account_date_idx",
            "account_id",
            text("internal_date DESC"),
            text("id DESC"),
        ),
        Index("messages_thread_idx", "thread_id"),
        Index("messages_from_idx", "from_addr"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    thread_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("threads.id", ondelete="SET NULL")
    )
    rfc822_message_id: Mapped[str | None] = mapped_column(Text)
    dedup_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    subject: Mapped[str | None] = mapped_column(Text)
    subject_norm: Mapped[str | None] = mapped_column(Text)
    from_name: Mapped[str | None] = mapped_column(Text)
    from_addr: Mapped[str | None] = mapped_column(Text)
    to_addrs: Mapped[list | None] = mapped_column(JSONB)
    cc_addrs: Mapped[list | None] = mapped_column(JSONB)
    bcc_addrs: Mapped[list | None] = mapped_column(JSONB)
    reply_to: Mapped[list | None] = mapped_column(JSONB)

    internal_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    has_attachments: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    snippet: Mapped[str | None] = mapped_column(Text)

    blob_sha256: Mapped[bytes | None] = mapped_column(LargeBinary)
    body_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="none")
    headers: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MailboxMessage(UUIDPk, Base):
    """Placement of one message in one folder (a message can live in multiple folders)."""

    __tablename__ = "mailbox_messages"
    __table_args__ = (
        Index(
            "mm_folder_uid_key",
            "folder_id",
            "remote_uid",
            unique=True,
            postgresql_where=text("remote_uid is not null"),
        ),
        Index(
            "mm_folder_item_key",
            "folder_id",
            "remote_item_id",
            unique=True,
            postgresql_where=text("remote_item_id is not null"),
        ),
        Index("mm_folder_unseen_idx", "folder_id", postgresql_where=text("not flag_seen")),
        Index("mm_message_idx", "message_id"),
    )

    folder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("folders.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    remote_uid: Mapped[int | None] = mapped_column(BigInteger)
    remote_item_id: Mapped[str | None] = mapped_column(Text)
    flag_seen: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    flag_flagged: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    flag_answered: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    flag_draft: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    flag_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    modseq: Mapped[int | None] = mapped_column(BigInteger)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MessageBody(Base):
    __tablename__ = "message_bodies"

    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True
    )
    body_text: Mapped[str | None] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)


class MessageSearch(Base):
    __tablename__ = "message_search"
    __table_args__ = (Index("message_search_tsv_idx", "tsv", postgresql_using="gin"),)

    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True
    )
    tsv: Mapped[str | None] = mapped_column(TSVECTOR)


class MessageRef(Base):
    __tablename__ = "message_refs"
    __table_args__ = (
        PrimaryKeyConstraint("message_id", "position", name="message_refs_pkey"),
        Index("message_refs_ref_idx", "ref"),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    ref: Mapped[str] = mapped_column(Text, nullable=False)


class Attachment(UUIDPk, Base):
    __tablename__ = "attachments"
    __table_args__ = (Index("attachments_message_idx", "message_id"),)

    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    part_path: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    content_id: Mapped[str | None] = mapped_column(Text)
    is_inline: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")


class Outbox(UUIDPk, Base):
    __tablename__ = "outbox"
    __table_args__ = (
        CheckConstraint(f"status in ({OUTBOX_STATUS_VALUES})", name="outbox_status_check"),
        Index("outbox_status_idx", "status", text("created_at DESC")),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_by_token_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="queued")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    mime_sha256: Mapped[bytes | None] = mapped_column(LargeBinary)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    # When a job starts sending this row. Used to atomically claim rows and recover a
    # stale claim if the sending process dies mid-flight.
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SyncRun(Base):
    __tablename__ = "sync_runs"
    __table_args__ = (Index("sync_runs_account_idx", "account_id", text("started_at DESC")),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("folders.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    added: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    updated: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    removed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)


class AutoconfigCache(Base):
    __tablename__ = "autoconfig_cache"

    domain: Mapped[str] = mapped_column(Text, primary_key=True)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
