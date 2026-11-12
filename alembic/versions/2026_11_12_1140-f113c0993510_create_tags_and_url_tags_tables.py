"""create tags and url_tags tables

Revision ID: f113c0993510
Revises: d488552c333d
Create Date: 2026-11-12 11:40:04.854761

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f113c0993510'
down_revision: Union[str, Sequence[str], None] = 'd488552c333d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create tags first, then the url_tags table that points to it."""
    op.create_table('tags',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=50), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tags')),
    sa.UniqueConstraint('name', name=op.f('uq_tags_name'))
    )
    # Case-insensitive uniqueness: "Marketing" and "marketing" collide
    op.create_index('uq_tags_name_lower', 'tags', [sa.literal_column('lower(name)')], unique=True)
    op.create_table('url_tags',
    sa.Column('url_id', sa.Integer(), nullable=False),
    sa.Column('tag_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['tag_id'], ['tags.id'], name=op.f('fk_url_tags_tag_id_tags'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['url_id'], ['urls.id'], name=op.f('fk_url_tags_url_id_urls'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('url_id', 'tag_id', name=op.f('pk_url_tags'))
    )
    op.create_index(op.f('ix_url_tags_tag_id'), 'url_tags', ['tag_id'], unique=False)


def downgrade() -> None:
    """Reverse order: url_tags points to tags, so it must go first."""
    op.drop_index(op.f('ix_url_tags_tag_id'), table_name='url_tags')
    op.drop_table('url_tags')
    op.drop_index('uq_tags_name_lower', table_name='tags')
    op.drop_table('tags')
