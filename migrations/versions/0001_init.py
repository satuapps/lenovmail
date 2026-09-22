# Lenovmail — authored by satuapps
"""init

Revision ID: 0001
Revises:
Create Date: 2026-09-22 01:59:27.986572
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "autoconfig_cache",
        sa.Column("domain", sa.Text(), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("domain"),
    )
    op.create_table(
        "users",
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(length=16), server_default="user", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("role in ('admin','user')", name="users_role_check"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "accounts",
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("email_address", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("status_detail", sa.Text(), nullable=True),
        sa.Column("sync_interval_s", sa.Integer(), server_default="300", nullable=False),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("provider in ('imap','graph')", name="accounts_provider_check"),
        sa.CheckConstraint(
            "status in ('active','auth_error','error','disabled')", name="accounts_status_check"
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "email_address", name="accounts_owner_email_key"),
    )
    op.create_table(
        "agent_tokens",
        sa.Column("owner_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("account_ids", postgresql.ARRAY(sa.UUID()), nullable=True),
        sa.Column("require_send_approval", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("send_limit_per_hour", sa.Integer(), server_default="20", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("agent_tokens_hash_key", "agent_tokens", ["token_hash"], unique=True)
    op.create_table(
        "agent_audit",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("token_id", sa.UUID(), nullable=True),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("tool", sa.String(length=64), nullable=False),
        sa.Column("params_digest", sa.Text(), nullable=True),
        sa.Column("target_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("outcome in ('ok','denied','error')", name="agent_audit_outcome_check"),
        sa.ForeignKeyConstraint(["token_id"], ["agent_tokens.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "agent_audit_created_idx",
        "agent_audit",
        [sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_table(
        "folders",
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("remote_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("delimiter", sa.Text(), nullable=True),
        sa.Column("role", sa.String(length=16), server_default="other", nullable=False),
        sa.Column("parent_id", sa.UUID(), nullable=True),
        sa.Column("uidvalidity", sa.BigInteger(), nullable=True),
        sa.Column("uidnext", sa.BigInteger(), nullable=True),
        sa.Column("highestmodseq", sa.BigInteger(), nullable=True),
        sa.Column("delta_link_enc", sa.LargeBinary(), nullable=True),
        sa.Column("sync_state", sa.String(length=16), server_default="idle", nullable=False),
        sa.Column("sync_error", sa.Text(), nullable=True),
        sa.Column("sync_pass", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_pinned", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "role in ('inbox','sent','drafts','trash','junk','archive','other')",
            name="folders_role_check",
        ),
        sa.CheckConstraint(
            "sync_state in ('idle','syncing','error')", name="folders_sync_state_check"
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_id"], ["folders.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "remote_id", name="folders_account_remote_key"),
    )
    op.create_table(
        "graph_settings",
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("home_account_id", sa.Text(), nullable=True),
        sa.Column("token_cache_enc", sa.LargeBinary(), nullable=True),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.Column("folders_delta_link_enc", sa.LargeBinary(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("account_id"),
    )
    op.create_table(
        "imap_settings",
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("security", sa.String(length=16), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("password_enc", sa.LargeBinary(), nullable=True),
        sa.Column("smtp_host", sa.Text(), nullable=True),
        sa.Column("smtp_port", sa.Integer(), nullable=True),
        sa.Column("smtp_security", sa.String(length=16), nullable=True),
        sa.Column("smtp_username", sa.Text(), nullable=True),
        sa.Column("smtp_password_enc", sa.LargeBinary(), nullable=True),
        sa.Column("append_to_sent", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "caps",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint("security in ('ssl','starttls','none')", name="imap_security_check"),
        sa.CheckConstraint(
            "smtp_security is null or smtp_security in ('ssl','starttls','none')",
            name="imap_smtp_security_check",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("account_id"),
    )
    op.create_table(
        "outbox",
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("created_by_token_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="queued", nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("mime_sha256", sa.LargeBinary(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "status in ('pending_approval','queued','sending','sent','failed')",
            name="outbox_status_check",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "outbox_status_idx",
        "outbox",
        ["status", sa.literal_column("created_at DESC")],
        unique=False,
    )
    op.create_table(
        "threads",
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("subject_norm", sa.Text(), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "threads_account_last_idx",
        "threads",
        ["account_id", sa.literal_column("last_message_at DESC")],
        unique=False,
    )
    op.create_table(
        "messages",
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("thread_id", sa.UUID(), nullable=True),
        sa.Column("rfc822_message_id", sa.Text(), nullable=True),
        sa.Column("dedup_hash", sa.LargeBinary(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("subject_norm", sa.Text(), nullable=True),
        sa.Column("from_name", sa.Text(), nullable=True),
        sa.Column("from_addr", sa.Text(), nullable=True),
        sa.Column("to_addrs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("cc_addrs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("bcc_addrs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reply_to", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("internal_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("has_attachments", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("blob_sha256", sa.LargeBinary(), nullable=True),
        sa.Column("body_state", sa.String(length=16), server_default="none", nullable=False),
        sa.Column("headers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.CheckConstraint(
            "body_state in ('none','partial','full')", name="messages_body_state_check"
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["thread_id"], ["threads.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "dedup_hash", name="messages_account_dedup_key"),
    )
    op.create_index(
        "messages_account_date_idx",
        "messages",
        ["account_id", sa.literal_column("internal_date DESC"), sa.literal_column("id DESC")],
        unique=False,
    )
    op.create_index("messages_from_idx", "messages", ["from_addr"], unique=False)
    op.create_index("messages_thread_idx", "messages", ["thread_id"], unique=False)
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("account_id", sa.UUID(), nullable=False),
        sa.Column("folder_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("added", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated", sa.Integer(), server_default="0", nullable=False),
        sa.Column("removed", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["folder_id"], ["folders.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "sync_runs_account_idx",
        "sync_runs",
        ["account_id", sa.literal_column("started_at DESC")],
        unique=False,
    )
    op.create_table(
        "attachments",
        sa.Column("message_id", sa.UUID(), nullable=False),
        sa.Column("part_path", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("content_id", sa.Text(), nullable=True),
        sa.Column("is_inline", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("attachments_message_idx", "attachments", ["message_id"], unique=False)
    op.create_table(
        "mailbox_messages",
        sa.Column("folder_id", sa.UUID(), nullable=False),
        sa.Column("message_id", sa.UUID(), nullable=False),
        sa.Column("remote_uid", sa.BigInteger(), nullable=True),
        sa.Column("remote_item_id", sa.Text(), nullable=True),
        sa.Column("flag_seen", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("flag_flagged", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("flag_answered", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("flag_draft", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("flag_deleted", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("modseq", sa.BigInteger(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.ForeignKeyConstraint(["folder_id"], ["folders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "mm_folder_item_key",
        "mailbox_messages",
        ["folder_id", "remote_item_id"],
        unique=True,
        postgresql_where=sa.text("remote_item_id is not null"),
    )
    op.create_index(
        "mm_folder_uid_key",
        "mailbox_messages",
        ["folder_id", "remote_uid"],
        unique=True,
        postgresql_where=sa.text("remote_uid is not null"),
    )
    op.create_index(
        "mm_folder_unseen_idx",
        "mailbox_messages",
        ["folder_id"],
        unique=False,
        postgresql_where=sa.text("not flag_seen"),
    )
    op.create_index("mm_message_idx", "mailbox_messages", ["message_id"], unique=False)
    op.create_table(
        "message_bodies",
        sa.Column("message_id", sa.UUID(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("message_id"),
    )
    op.create_table(
        "message_refs",
        sa.Column("message_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("ref", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("message_id", "position", name="message_refs_pkey"),
    )
    op.create_index("message_refs_ref_idx", "message_refs", ["ref"], unique=False)
    op.create_table(
        "message_search",
        sa.Column("message_id", sa.UUID(), nullable=False),
        sa.Column("tsv", postgresql.TSVECTOR(), nullable=True),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("message_id"),
    )
    op.create_index(
        "message_search_tsv_idx", "message_search", ["tsv"], unique=False, postgresql_using="gin"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("message_search_tsv_idx", table_name="message_search", postgresql_using="gin")
    op.drop_table("message_search")
    op.drop_index("message_refs_ref_idx", table_name="message_refs")
    op.drop_table("message_refs")
    op.drop_table("message_bodies")
    op.drop_index("mm_message_idx", table_name="mailbox_messages")
    op.drop_index(
        "mm_folder_unseen_idx",
        table_name="mailbox_messages",
        postgresql_where=sa.text("not flag_seen"),
    )
    op.drop_index(
        "mm_folder_uid_key",
        table_name="mailbox_messages",
        postgresql_where=sa.text("remote_uid is not null"),
    )
    op.drop_index(
        "mm_folder_item_key",
        table_name="mailbox_messages",
        postgresql_where=sa.text("remote_item_id is not null"),
    )
    op.drop_table("mailbox_messages")
    op.drop_index("attachments_message_idx", table_name="attachments")
    op.drop_table("attachments")
    op.drop_index("sync_runs_account_idx", table_name="sync_runs")
    op.drop_table("sync_runs")
    op.drop_index("messages_thread_idx", table_name="messages")
    op.drop_index("messages_from_idx", table_name="messages")
    op.drop_index("messages_account_date_idx", table_name="messages")
    op.drop_table("messages")
    op.drop_index("threads_account_last_idx", table_name="threads")
    op.drop_table("threads")
    op.drop_index("outbox_status_idx", table_name="outbox")
    op.drop_table("outbox")
    op.drop_table("imap_settings")
    op.drop_table("graph_settings")
    op.drop_table("folders")
    op.drop_index("agent_audit_created_idx", table_name="agent_audit")
    op.drop_table("agent_audit")
    op.drop_index("agent_tokens_hash_key", table_name="agent_tokens")
    op.drop_table("agent_tokens")
    op.drop_table("accounts")
    op.drop_table("users")
    op.drop_table("autoconfig_cache")
