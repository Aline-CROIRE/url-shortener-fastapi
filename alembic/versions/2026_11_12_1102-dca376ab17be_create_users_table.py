"""create users table

Revision ID: dca376ab17be
Revises:
Create Date: 2026-11-12 11:02:16.783953

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'dca376ab17be'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the users table (and, automatically, the user_tier ENUM type)."""
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('username', sa.String(length=50), nullable=False),
    sa.Column('hashed_password', sa.String(length=255), nullable=False),
    sa.Column('is_premium', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('tier', sa.Enum('free', 'premium', 'admin', name='user_tier'), server_default='free', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('email', name=op.f('uq_users_email')),
    sa.UniqueConstraint('username', name=op.f('uq_users_username'))
    )


def downgrade() -> None:
    """Drop the users table, then the user_tier ENUM type it used."""
    op.drop_table('users')
    # Autogenerate forgets this: drop_table does not remove the ENUM type,
    # and the next upgrade would fail with "type user_tier already exists".
    op.execute("DROP TYPE IF EXISTS user_tier")
