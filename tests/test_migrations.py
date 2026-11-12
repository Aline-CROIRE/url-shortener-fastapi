import asyncio

from alembic import command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from tests.conftest import alembic_config


async def tables_and_types(database_url: str) -> tuple[set[str], set[str]]:
    """Return (table names, ENUM type names) in the public schema."""
    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.connect() as connection:
        tables = (
            await connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        ).scalars()
        types = (
            await connection.execute(text("SELECT typname FROM pg_type WHERE typtype = 'e'"))
        ).scalars()
        result = set(tables), set(types)
    await engine.dispose()
    return result


def test_downgrade_to_base_removes_everything_and_upgrade_rebuilds() -> None:
    url = get_settings().test_database_url
    config = alembic_config(url)

    command.downgrade(config, "base")  # undo every migration
    tables, types = asyncio.run(tables_and_types(url))
    assert tables == {"alembic_version"}  # only the "sign on the door" is left
    assert types == set()  # no forgotten ENUM types

    command.upgrade(config, "head")  # build everything again
    tables, types = asyncio.run(tables_and_types(url))
    assert {"users", "urls"} <= tables
    assert "user_tier" in types
