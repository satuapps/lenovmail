# Lenovmail — authored by satuapps (satuapps.com)
"""message_values + messages.values_mined_at

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-22 13:40:00.000000

Values worth acting on — one-time codes, password reset links, license keys, promo codes —
are extracted once at ingest instead of being re-scanned on every query: a regex pass over
every stored body cannot be indexed, so filtering by kind would degrade with mailbox size.
`messages.values_mined_at` marks the rows that have been through the extractor, which is what
lets the backfill job find the ones stored before mining existed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "message_values",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "message_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False, server_default="70"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind in ('otp','reset_link','key','promo')", name="message_values_kind_check"
        ),
        sa.CheckConstraint("confidence between 0 and 100", name="message_values_confidence_check"),
        sa.UniqueConstraint("message_id", "kind", "value", name="message_values_unique"),
    )
    op.create_index("message_values_kind_idx", "message_values", ["kind", "message_id"])
    op.add_column(
        "messages", sa.Column("values_mined_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "messages_values_pending_idx",
        "messages",
        ["id"],
        postgresql_where=sa.text("values_mined_at is null and body_state <> 'none'"),
    )


def downgrade() -> None:
    op.drop_index("messages_values_pending_idx", table_name="messages")
    op.drop_column("messages", "values_mined_at")
    op.drop_index("message_values_kind_idx", table_name="message_values")
    op.drop_table("message_values")
