"""keep is_premium in sync with tier

Revision ID: dfe41766eb04
Revises: 82a55000dc8d
Create Date: 2026-11-13 09:41:00

is_premium must always match tier (premium and admin have premium features).
Two steps: first repair existing rows (data), then add the CHECK rule (schema),
so the rule cannot fail on old data. Autogenerate does not detect new CHECK
constraints on existing tables, so this migration is written by hand.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'dfe41766eb04'
down_revision: Union[str, Sequence[str], None] = '82a55000dc8d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Repair contradicting rows, then forbid contradictions."""
    op.execute("UPDATE users SET is_premium = (tier IN ('premium', 'admin'))")
    op.create_check_constraint(
        # op.f(): the exact final name (the model's naming convention gives the same)
        op.f('ck_users_is_premium_matches_tier'),
        'users',
        "is_premium = (tier IN ('premium', 'admin'))",
    )


def downgrade() -> None:
    """Remove the rule (the repaired data stays correct)."""
    op.drop_constraint(op.f('ck_users_is_premium_matches_tier'), 'users', type_='check')
