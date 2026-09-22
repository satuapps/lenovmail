# Lenovmail — authored by satuapps (satuapps.com)
"""API request/response schemas. The JSON shape is intentionally stable since it's used
by both the GUI and agents.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Email = Annotated[
    str, StringConstraints(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
]
Security = Literal["ssl", "starttls", "none"]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- auth -----------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: Email
    password: str = Field(min_length=1, max_length=1024)


class UserOut(ORMModel):
    id: uuid.UUID
    email: str
    role: str
    is_active: bool
    created_at: datetime


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=10, max_length=1024)


class SessionOut(BaseModel):
    """One browser session. `id` is a digest of the session token, never the token itself."""

    id: str
    created_at: datetime
    last_seen_at: datetime | None = None
    ip: str | None = None
    user_agent: str | None = None
    current: bool = False


# --- accounts & autoconfig --------------------------------------------------------


class ServerIn(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    security: Security = "ssl"
    username: str | None = None
    password: str | None = None


class ServerOut(BaseModel):
    host: str
    port: int
    security: str
    auth: str
    username: str | None = None


class DiscoverRequest(BaseModel):
    email: Email
    password: str | None = None


class DiscoverOut(BaseModel):
    source: str
    provider: str
    oauth_required: bool
    imap: ServerOut | None = None
    smtp: ServerOut | None = None
    warnings: list[str] = Field(default_factory=list)


class AccountCreate(BaseModel):
    email_address: Email
    display_name: str | None = None
    provider: Literal["auto", "imap", "graph"] = "auto"
    password: str | None = None
    imap: ServerIn | None = None
    smtp: ServerIn | None = None
    sync_interval_s: int | None = Field(default=None, ge=60, le=86_400)


class AccountUpdate(BaseModel):
    display_name: str | None = None
    sync_interval_s: int | None = Field(default=None, ge=60, le=86_400)
    status: Literal["active", "disabled"] | None = None
    password: str | None = None


class AccountOut(ORMModel):
    id: uuid.UUID
    email_address: str
    display_name: str | None
    provider: str
    status: str
    status_detail: str | None
    sync_interval_s: int
    last_sync_at: datetime | None
    unread: int = 0
    total: int = 0
    invalid_since: datetime | None = None
    # When the janitor would retire this account, derived from `invalid_since` + grace.
    retire_at: datetime | None = None
    retire_action: str | None = None
    retire_warning: bool = False
    # Only set when creating a Microsoft account: the consent URL the user must open.
    oauth_url: str | None = None


class AccountTestOut(BaseModel):
    imap: str | None = None
    smtp: str | None = None
    graph: str | None = None
    folders: int | None = None


# --- folders & messages -----------------------------------------------------------


class FolderOut(ORMModel):
    id: uuid.UUID
    remote_id: str
    name: str
    role: str
    parent_id: uuid.UUID | None
    sync_state: str
    sync_error: str | None = None
    last_synced_at: datetime | None
    unread: int = 0
    total: int = 0


class AddressOut(BaseModel):
    name: str | None = None
    addr: str


class MessageOut(ORMModel):
    id: uuid.UUID
    account_id: uuid.UUID
    thread_id: uuid.UUID | None
    subject: str | None
    from_name: str | None
    from_addr: str | None
    to: list[AddressOut] = Field(default_factory=list)
    snippet: str | None
    internal_date: datetime | None
    sent_date: datetime | None
    has_attachments: bool
    body_state: str
    size_bytes: int | None
    seen: bool = False
    flagged: bool = False
    folder_ids: list[uuid.UUID] = Field(default_factory=list)
    # Kinds of mined values this message holds; the values themselves need their own call.
    value_kinds: list[str] = Field(default_factory=list)
    # Raw `Message-ID` header: used by GUI/agents to build replies (`in_reply_to`).
    rfc822_message_id: str | None = None


class MessageValueOut(BaseModel):
    id: uuid.UUID
    kind: str
    # Masked unless the caller asked for a reveal, which is written to `agent_audit`.
    value: str
    confidence: int
    revealed: bool = False


class MessagePage(BaseModel):
    items: list[MessageOut]
    next_cursor: str | None = None


class AttachmentOut(ORMModel):
    id: uuid.UUID
    filename: str | None
    mime_type: str | None
    size_bytes: int | None
    is_inline: bool
    content_id: str | None = None


class BodyOut(BaseModel):
    body_state: str
    text: str | None = None
    html: str | None = None
    attachments: list[AttachmentOut] = Field(default_factory=list)


class FlagUpdate(BaseModel):
    seen: bool | None = None
    flagged: bool | None = None


class MoveRequest(BaseModel):
    folder_id: uuid.UUID


class ThreadOut(BaseModel):
    thread_id: uuid.UUID
    subject: str | None = None
    message_count: int
    items: list[MessageOut]


# --- outbox ---------------------------------------------------------------------


class OutboxAttachment(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = "application/octet-stream"
    content_b64: str


class OutboxPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    sender: Email = Field(alias="from")
    to: list[Email] = Field(min_length=1)
    cc: list[Email] = Field(default_factory=list)
    bcc: list[Email] = Field(default_factory=list)
    subject: str = Field(default="", max_length=998)
    text: str | None = None
    html: str | None = None
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    attachments: list[OutboxAttachment] = Field(default_factory=list)


class OutboxCreate(BaseModel):
    account_id: uuid.UUID
    payload: OutboxPayload
    requires_approval: bool = False


class OutboxOut(ORMModel):
    id: uuid.UUID
    account_id: uuid.UUID
    status: str
    payload: dict[str, Any]
    attempts: int
    last_error: str | None
    created_at: datetime
    sent_at: datetime | None


# --- stats & ops ------------------------------------------------------------------


class ProviderStats(BaseModel):
    provider: str
    accounts_total: int = 0
    accounts_active: int = 0
    # `error` is a server that did not answer; `auth_error` is a credential the server
    # refused. They are counted apart because only the second one needs a human.
    accounts_unreachable: int = 0
    accounts_auth_rejected: int = 0
    accounts_disabled: int = 0
    messages: int = 0
    retire_warnings: int = 0


class OverviewOut(BaseModel):
    accounts_total: int = 0
    accounts_active: int = 0
    accounts_unreachable: int = 0
    accounts_auth_rejected: int = 0
    accounts_disabled: int = 0
    messages_total: int = 0
    unread_total: int = 0
    providers: list[ProviderStats] = Field(default_factory=list)


class ProviderOps(BaseModel):
    provider: str
    accounts_total: int = 0
    sync_runs_24h: int = 0
    sync_failures_24h: int = 0
    sent_last_hour: int = 0
    send_limit_per_hour: int
    max_connections_per_account: int | None = None
    poll_interval_s: int | None = None


class JanitorOut(BaseModel):
    grace_days: int
    action: str
    check_batch: int
    token_retention_days: int
    audit_retention_days: int
    next_retire_at: datetime | None = None


class OpsSummaryOut(BaseModel):
    unreachable: list[AccountOut] = Field(default_factory=list)
    auth_rejected: list[AccountOut] = Field(default_factory=list)
    janitor: JanitorOut
    providers: list[ProviderOps] = Field(default_factory=list)


# --- agent ----------------------------------------------------------------------


class AgentTokenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(default_factory=lambda: ["mail.read"])
    account_ids: list[uuid.UUID] | None = None
    require_send_approval: bool = True
    send_limit_per_hour: int | None = Field(default=None, ge=0, le=10_000)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


class AgentTokenOut(ORMModel):
    id: uuid.UUID
    name: str
    scopes: list[str]
    account_ids: list[uuid.UUID] | None
    require_send_approval: bool
    send_limit_per_hour: int
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class AgentTokenCreated(AgentTokenOut):
    # Shown once at creation time; its hash can't be reversed.
    token: str


class AgentAuditOut(ORMModel):
    id: int
    token_id: uuid.UUID | None
    user_id: uuid.UUID | None
    tool: str
    outcome: str
    error: str | None
    target_ids: list[Any] | None
    created_at: datetime


class HealthOut(BaseModel):
    status: str
    database: bool
    redis: bool
    version: str
