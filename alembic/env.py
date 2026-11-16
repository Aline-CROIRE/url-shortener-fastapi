"""Alembic environment: tells Alembic WHICH database and WHICH models to use."""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app import models  # noqa: F401  (importing registers every model on Base.metadata)
from app.core.config import get_settings
from app.db.base import Base

# The Alembic Config object: gives access to the values in alembic.ini
config = context.config

# Use alembic.ini's [logger_*] sections for Alembic's console output.
# disable_existing_loggers=False: keep the app's own loggers working when the
# tests run migrations in the same process.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Database URL: a URL set by code (the tests set the test database) wins;
# otherwise use DATABASE_URL from the app settings (.env / environment variables).
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", get_settings().database_url)

# The catalogue of all our tables: autogenerate compares it with the real database
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """'Offline' mode: print the SQL instead of running it (alembic upgrade head --sql)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,  # also notice column TYPE changes, e.g. String(50) -> String(100)
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """'Online' mode: connect with our async engine and run the migrations."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # a one-off script: no need to keep connections
    )
    async with connectable.connect() as connection:
        # Alembic itself is synchronous; run_sync lets it use the async connection
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
