# Lenovmail — authored by satuapps
"""Mail account model: `accounts` + per-provider settings (`imap_settings`, `graph_settings`)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Timestamped, UUIDPk


class Account(UUIDPk, Timestamped, Base):
    __tablename__ = "accounts"
    __table_args__ = (
        CheckConstraint("provider in ('imap','graph')", name="accounts_provider_check"),
        CheckConstraint(
            "status in ('active','auth_error','error','disabled')", name="accounts_status_check"
        ),
        UniqueConstraint("owner_id", "email_address", name="accounts_owner_email_key"),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    display_name: Mapped[str | None] = mapped_column(Text)
    email_address: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    status_detail: Mapped[str | None] = mapped_column(Text)
    sync_interval_s: Mapped[int] = mapped_column(Integer, nullable=False, server_default="300")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # First confirmed credential rejection; the janitor measures its grace period from here.
    invalid_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ImapSettings(Base):
    __tablename__ = "imap_settings"
    __table_args__ = (
        CheckConstraint("security in ('ssl','starttls','none')", name="imap_security_check"),
        CheckConstraint(
            "smtp_security is null or smtp_security in ('ssl','starttls','none')",
            name="imap_smtp_security_check",
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    host: Mapped[str] = mapped_column(Text, nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    security: Mapped[str] = mapped_column(String(16), nullable=False)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    password_enc: Mapped[bytes | None] = mapped_column(LargeBinary)

    smtp_host: Mapped[str | None] = mapped_column(Text)
    smtp_port: Mapped[int | None] = mapped_column(Integer)
    smtp_security: Mapped[str | None] = mapped_column(String(16))
    smtp_username: Mapped[str | None] = mapped_column(Text)
    smtp_password_enc: Mapped[bytes | None] = mapped_column(LargeBinary)

    append_to_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    caps: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))


class GraphSettings(Base):
    __tablename__ = "graph_settings"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    home_account_id: Mapped[str | None] = mapped_column(Text)
    token_cache_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    folders_delta_link_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
