"""Which database schema this code expects, and which one the database has.

Alembic writes the revision it applied last into the alembic_version table. The code
expects the newest revision in alembic/versions (the "head"). When they differ, the
migration job has not run yet: this instance must not serve requests (/health/startup).
"""

from functools import lru_cache
from pathlib import Path

from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# alembic/ next to app/ (in the Docker image: /app/alembic)
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "alembic"


@lru_cache
def expected_schema_head() -> str:
    """Return the newest migration revision in alembic/versions (read once per process).

    Raises:
        alembic.util.exc.CommandError: If there are two heads (two unmerged branches).
    """
    head = ScriptDirectory(str(MIGRATIONS_DIR)).get_current_head()
    if head is None:
        raise RuntimeError(f"No migrations found in {MIGRATIONS_DIR}")
    return head


async def current_schema_revision(session: AsyncSession) -> str | None:
    """Return the revision the database is at (None before the first migration)."""
    return await session.scalar(text("SELECT version_num FROM alembic_version"))
