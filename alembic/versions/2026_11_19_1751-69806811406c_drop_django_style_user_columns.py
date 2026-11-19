"""drop django style user columns

Revision ID: 69806811406c
Revises: dfe41766eb04
Create Date: 2026-11-19 17:51:47.078191

Removes the columns copied from Django's AbstractUser that this FastAPI app never
uses: first_name, last_name, is_staff and is_superuser. "Is this user an admin?"
is answered by tier = 'admin' alone, so two flags can no longer disagree with it.
username, is_active and last_login stay: the app uses them.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "69806811406c"
down_revision: Union[str, Sequence[str], None] = "dfe41766eb04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop the four unused columns."""
    op.drop_column("users", "is_superuser")
    op.drop_column("users", "is_staff")
    op.drop_column("users", "last_name")
    op.drop_column("users", "first_name")


def downgrade() -> None:
    """Add the four columns back.

    The names come back empty (the old values are gone). is_staff and is_superuser
    are rebuilt from the tier: true for admins, false for everyone else.
    """
    op.add_column(
        "users", sa.Column("first_name", sa.String(length=150), server_default="", nullable=False)
    )
    op.add_column(
        "users", sa.Column("last_name", sa.String(length=150), server_default="", nullable=False)
    )
    op.add_column(
        "users",
        sa.Column("is_staff", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "users",
        sa.Column("is_superuser", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.execute("UPDATE users SET is_staff = true, is_superuser = true WHERE tier = 'admin'")
