"""create urls table

Revision ID: 2ccea71a651f
Revises: dca376ab17be
Create Date: 2026-11-12 11:21:10.122601

Existing Module 5 URLs lived in memory/Redis, not in PostgreSQL, so this table
starts empty and owner_id can be NOT NULL right away (no backfill needed).
If the table already had rows, the safe pattern would be three steps:
add owner_id as nullable -> fill it with a default owner -> set NOT NULL.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2ccea71a651f'
down_revision: Union[str, Sequence[str], None] = 'dca376ab17be'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the urls table, owned by users, with its indexes."""
    op.create_table('urls',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('original_url', sa.Text(), nullable=False),
    sa.Column('short_code', sa.String(length=10), nullable=False),
    sa.Column('custom_alias', sa.String(length=10), nullable=True),
    sa.Column('owner_id', sa.Integer(), nullable=False),
    sa.Column('is_active', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('title', sa.String(length=255), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('favicon', sa.Text(), nullable=True),
    sa.Column('click_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('click_count >= 0', name=op.f('ck_urls_click_count_non_negative')),
    sa.CheckConstraint('custom_alias IS NULL OR custom_alias = short_code', name=op.f('ck_urls_custom_alias_matches_short_code')),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], name=op.f('fk_urls_owner_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_urls')),
    sa.UniqueConstraint('custom_alias', name=op.f('uq_urls_custom_alias'))
    )
    op.create_index('ix_urls_created_at', 'urls', ['created_at'], unique=False)
    op.create_index(op.f('ix_urls_owner_id'), 'urls', ['owner_id'], unique=False)
    op.create_index(op.f('ix_urls_short_code'), 'urls', ['short_code'], unique=True)


def downgrade() -> None:
    """Drop the indexes, then the urls table."""
    op.drop_index(op.f('ix_urls_short_code'), table_name='urls')
    op.drop_index(op.f('ix_urls_owner_id'), table_name='urls')
    op.drop_index('ix_urls_created_at', table_name='urls')
    op.drop_table('urls')
