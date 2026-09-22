# Lenovmail — authored by satuapps
"""Agent access model: scoped tokens and audit log."""

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
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, UUIDPk


class AgentToken(UUIDPk, Base):
    __tablename__ = "agent_tokens"
    __table_args__ = (Index("agent_tokens_hash_key", "token_hash", unique=True),)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")
    # NULL = all accounts owned by the owner.
    account_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(UUID(as_uuid=True)))
    require_send_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    send_limit_per_hour: Mapped[int] = mapped_column(Integer, nullable=False, server_default="20")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class AgentAudit(Base):
    __tablename__ = "agent_audit"
    __table_args__ = (
        CheckConstraint("outcome in ('ok','denied','error')", name="agent_audit_outcome_check"),
        Index("agent_audit_created_idx", text("created_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    token_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_tokens.id", ondelete="SET NULL")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    tool: Mapped[str] = mapped_column(String(64), nullable=False)
    params_digest: Mapped[str | None] = mapped_column(Text)
    target_ids: Mapped[list | None] = mapped_column(JSONB)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
