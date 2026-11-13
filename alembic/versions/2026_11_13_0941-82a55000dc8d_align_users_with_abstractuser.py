"""align users with abstractuser

Revision ID: 82a55000dc8d
Revises: 0109315884f6
Create Date: 2026-11-13 09:41:00

Adds the remaining fields of Django's AbstractUser (first_name, last_name,
is_active, is_staff, is_superuser, last_login) and widens username to 150
characters. Every new column has a server default (or allows NULL), so existing
users need no backfill. date_joined is not a column: it is the existing created_at.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '82a55000dc8d'
down_revision: Union[str, Sequence[str], None] = '0109315884f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the AbstractUser columns and widen username."""
    op.add_column('users', sa.Column('first_name', sa.String(length=150), server_default='', nullable=False))
    op.add_column('users', sa.Column('last_name', sa.String(length=150), server_default='', nullable=False))
    op.add_column('users', sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False))
    op.add_column('users', sa.Column('is_staff', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('users', sa.Column('is_superuser', sa.Boolean(), server_default=sa.text('false'), nullable=False))
    op.add_column('users', sa.Column('last_login', sa.DateTime(timezone=True), nullable=True))
    op.alter_column('users', 'username', type_=sa.String(length=150), existing_type=sa.String(length=50), existing_nullable=False)


def downgrade() -> None:
    """Drop the added columns and shrink username back to 50 characters.

    Note: shrinking fails if a username longer than 50 characters exists; that is
    deliberate (better than silently cutting someone's username).
    """
    op.alter_column('users', 'username', type_=sa.String(length=50), existing_type=sa.String(length=150), existing_nullable=False)
    op.drop_column('users', 'last_login')
    op.drop_column('users', 'is_superuser')
    op.drop_column('users', 'is_staff')
    op.drop_column('users', 'is_active')
    op.drop_column('users', 'last_name')
    op.drop_column('users', 'first_name')
