# Lenovmail — authored by satuapps (satuapps.com)
"""outbox.locked_at

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-22 02:35:00.000000

Adds a claim column to `outbox`. Without this column, two delivery jobs running
concurrently could pick up the same `queued` row and send the message twice.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("outbox", sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("outbox", "locked_at")
