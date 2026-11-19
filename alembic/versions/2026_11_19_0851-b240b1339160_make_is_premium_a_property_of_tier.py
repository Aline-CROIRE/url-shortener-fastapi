"""make is_premium a property of tier

Revision ID: b240b1339160
Revises: 69806811406c
Create Date: 2026-11-19 08:51:00

tier becomes the single source of truth: is_premium is now a read-only Python
property (User.is_premium), so the column and the CHECK rule that kept the two
in sync are no longer needed. No data is lost: is_premium can always be worked
out again from tier, which is exactly what downgrade() does.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b240b1339160"
down_revision: Union[str, Sequence[str], None] = "69806811406c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CHECK_NAME = "ck_users_is_premium_matches_tier"
CHECK_RULE = "is_premium = (tier IN ('premium', 'admin'))"


def upgrade() -> None:
    """Drop the CHECK rule first (it uses the column), then the column."""
    op.drop_constraint(op.f(CHECK_NAME), "users", type_="check")
    op.drop_column("users", "is_premium")


def downgrade() -> None:
    """Bring the column back, fill it from tier, then bring the rule back."""
    op.add_column(
        "users",
        sa.Column("is_premium", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.execute("UPDATE users SET is_premium = (tier IN ('premium', 'admin'))")
    op.create_check_constraint(op.f(CHECK_NAME), "users", CHECK_RULE)
