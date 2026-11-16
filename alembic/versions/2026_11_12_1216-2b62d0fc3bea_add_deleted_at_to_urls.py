"""add deleted_at to urls

Revision ID: 2b62d0fc3bea
Revises: 5de473cbc932
Create Date: 2026-11-12 12:16:48.247702

Soft delete: DELETE /api/v1/urls/{code} sets deleted_at instead of removing the
row, so the short code is never reused and its clicks are kept.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "2b62d0fc3bea"
down_revision: Union[str, Sequence[str], None] = "5de473cbc932"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the nullable column: existing rows get NULL (= not deleted), no backfill needed."""
    op.add_column("urls", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Drop the column, but first switch deleted URLs off.

    Without this step, every URL a user deleted would start redirecting again
    the moment deleted_at disappears.
    """
    op.execute("UPDATE urls SET is_active = false WHERE deleted_at IS NOT NULL")
    op.drop_column("urls", "deleted_at")
