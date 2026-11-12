"""create clicks table

Revision ID: d488552c333d
Revises: 2ccea71a651f
Create Date: 2026-11-12 11:32:40.983791

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd488552c333d'
down_revision: Union[str, Sequence[str], None] = '2ccea71a651f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the clicks table and its (url_id, clicked_at) index."""
    op.create_table('clicks',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('url_id', sa.Integer(), nullable=False),
    sa.Column('clicked_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('ip_address', postgresql.INET(), nullable=True),
    sa.Column('user_agent', sa.Text(), nullable=True),
    sa.Column('referrer', sa.Text(), nullable=True),
    sa.Column('country', sa.String(length=2), nullable=True),
    sa.Column('city', sa.String(length=100), nullable=True),
    sa.ForeignKeyConstraint(['url_id'], ['urls.id'], name=op.f('fk_clicks_url_id_urls'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_clicks'))
    )
    op.create_index('ix_clicks_url_id_clicked_at', 'clicks', ['url_id', 'clicked_at'], unique=False)


def downgrade() -> None:
    """Drop the index, then the clicks table."""
    op.drop_index('ix_clicks_url_id_clicked_at', table_name='clicks')
    op.drop_table('clicks')
