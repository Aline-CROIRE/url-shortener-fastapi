"""seed default tags

Revision ID: 5de473cbc932
Revises: f113c0993510
Create Date: 2026-11-12 11:52:13.295053

A DATA migration: it adds rows, it does not change any table.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5de473cbc932'
down_revision: Union[str, Sequence[str], None] = 'f113c0993510'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_TAGS = ["Marketing", "Social", "Email", "Ads", "Events", "Personal"]

# A lightweight copy of the tags table with only the column we need.
# We do NOT import app.models.Tag: the model will change over time, but this
# migration must keep working exactly as written (a frozen snapshot).
tags_table = sa.table("tags", sa.column("name", sa.String))


def upgrade() -> None:
    """Insert the default tags (id, created_at, updated_at are filled by Postgres)."""
    op.bulk_insert(tags_table, [{"name": name} for name in DEFAULT_TAGS])


def downgrade() -> None:
    """Remove exactly the tags this migration added.

    Warning: if URLs already use one of these tags, their url_tags links are
    deleted too (ON DELETE CASCADE). Downgrading a data migration on a live
    database should be a deliberate decision.
    """
    op.execute(tags_table.delete().where(tags_table.c.name.in_(DEFAULT_TAGS)))
