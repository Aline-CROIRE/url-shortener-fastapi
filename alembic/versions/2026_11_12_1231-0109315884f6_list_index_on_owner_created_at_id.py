"""list index on owner created_at id

Revision ID: 0109315884f6
Revises: 2b62d0fc3bea
Create Date: 2026-11-12 12:31:30.558232

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0109315884f6"
down_revision: Union[str, Sequence[str], None] = "2b62d0fc3bea"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Replace ix_urls_owner_id with the keyset-pagination index.

    Create the new index FIRST, then drop the old one, so owner_id lookups are
    never without an index. (On a very large production table you would use
    CREATE INDEX CONCURRENTLY, which does not block writes but cannot run
    inside a transaction.)
    """
    op.create_index(
        "ix_urls_owner_id_created_at_id", "urls", ["owner_id", "created_at", "id"], unique=False
    )
    op.drop_index(op.f("ix_urls_owner_id"), table_name="urls")


def downgrade() -> None:
    """Same order rule in reverse: restore the old index first."""
    op.create_index(op.f("ix_urls_owner_id"), "urls", ["owner_id"], unique=False)
    op.drop_index("ix_urls_owner_id_created_at_id", table_name="urls")
