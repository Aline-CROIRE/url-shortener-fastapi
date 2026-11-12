"""Tests for the migrations themselves (the Alembic "logbook").

Each test leaves the test database at "head" again, so other tests are not affected.
"""

import asyncio
from typing import Any

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app import models  # noqa: F401  (registers every model on Base.metadata)
from app.core.config import get_settings
from app.db.base import Base
from tests.conftest import alembic_config

DEFAULT_TAGS = {"Marketing", "Social", "Email", "Ads", "Events", "Personal"}


async def run_query(database_url: str, sql: str) -> set[Any]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.connect() as connection:
        values = set((await connection.execute(text(sql))).scalars())
    await engine.dispose()
    return values


def tables(url: str) -> set[str]:
    return asyncio.run(run_query(url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))


def enum_types(url: str) -> set[str]:
    return asyncio.run(run_query(url, "SELECT typname FROM pg_type WHERE typtype = 'e'"))


def tag_names(url: str) -> set[str]:
    return asyncio.run(run_query(url, "SELECT name FROM tags"))


def test_downgrade_to_base_removes_everything_and_upgrade_rebuilds() -> None:
    url = get_settings().test_database_url
    config = alembic_config(url)

    command.downgrade(config, "base")  # undo every migration
    assert tables(url) == {"alembic_version"}  # only the "sign on the door" is left
    assert enum_types(url) == set()  # no forgotten ENUM types

    command.upgrade(config, "head")  # build everything again
    assert {"users", "urls", "clicks", "tags", "url_tags"} <= tables(url)
    assert "user_tier" in enum_types(url)


def test_seed_migration_adds_default_tags_and_downgrade_removes_them() -> None:
    url = get_settings().test_database_url
    config = alembic_config(url)
    assert DEFAULT_TAGS <= tag_names(url)  # at head: seeded

    command.downgrade(config, "-1")  # undo only the data migration
    assert tag_names(url).isdisjoint(DEFAULT_TAGS)  # removed
    assert "tags" in tables(url)  # ...but the table itself is still there

    command.upgrade(config, "head")
    assert DEFAULT_TAGS <= tag_names(url)  # back again


def test_there_is_exactly_one_head() -> None:
    # Two heads = two migrations claim the same parent (e.g. after merging two
    # branches). "alembic upgrade head" would then refuse to run.
    script = ScriptDirectory.from_config(alembic_config(get_settings().test_database_url))
    assert len(script.get_heads()) == 1


def test_models_match_the_migrations() -> None:
    """The same check as "alembic check": catches a model change without a migration."""

    def differences(connection: Connection) -> list[Any]:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        return compare_metadata(context, Base.metadata)

    async def run() -> list[Any]:
        engine = create_async_engine(get_settings().test_database_url, poolclass=NullPool)
        async with engine.connect() as connection:
            diff = await connection.run_sync(differences)
        await engine.dispose()
        return diff

    assert asyncio.run(run()) == []
