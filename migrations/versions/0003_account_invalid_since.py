# Lenovmail — authored by satuapps (satuapps.com)
"""accounts.invalid_since

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-22 09:10:00.000000

The janitor needs to know how long an account has been rejecting logins. `status_detail`
carries the reason but no timestamp, and `updated_at` moves on every unrelated write, so the
grace period gets its own column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("invalid_since", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("accounts", "invalid_since")
