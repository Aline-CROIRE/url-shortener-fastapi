"""Streaming replication against the REAL replica container.

Start it first:  docker compose --profile replica up -d db-replica
The replica copies the whole primary server, including the test database.
If it is not reachable, every test here is skipped.
"""

import asyncio
import os
import time
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

REPLICA_TEST_URL = os.getenv(
    "REPLICA_TEST_URL", "postgresql+asyncpg://postgres:postgres@127.0.0.1:5434/url_shortener_test"
)
TAG = "ReplicationProbe"


def replica_is_reachable() -> bool:
    async def ping() -> bool:
        engine = create_async_engine(
            REPLICA_TEST_URL, poolclass=NullPool, connect_args={"timeout": 2}
        )
        try:
            async with engine.connect() as connection:
                return bool(await connection.scalar(text("SELECT pg_is_in_recovery()")))
        except (DBAPIError, OSError):
            return False
        finally:
            await engine.dispose()

    return asyncio.run(ping())


pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.skipif(not replica_is_reachable(), reason=f"No replica at {REPLICA_TEST_URL}"),
]


@pytest.fixture
async def primary() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().test_database_url, poolclass=NullPool)
    yield engine
    async with engine.begin() as connection:  # always clean up the probe row
        await connection.execute(text("DELETE FROM tags WHERE name = :name"), {"name": TAG})
    await engine.dispose()


@pytest.fixture
async def replica() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(REPLICA_TEST_URL, poolclass=NullPool)
    yield engine
    await engine.dispose()


async def probe_count(replica: AsyncEngine) -> int:
    async with replica.connect() as connection:
        return await connection.scalar(
            text("SELECT count(*) FROM tags WHERE name = :name"), {"name": TAG}
        )


async def test_a_committed_write_appears_on_the_replica(
    primary: AsyncEngine, replica: AsyncEngine
) -> None:
    async with primary.begin() as connection:  # COMMIT on the primary
        await connection.execute(text("INSERT INTO tags (name) VALUES (:name)"), {"name": TAG})

    started = time.perf_counter()
    while await probe_count(replica) == 0:  # replication is asynchronous: wait a little
        assert time.perf_counter() - started < 5, "the change never reached the replica"
        await asyncio.sleep(0.05)
    lag_ms = (time.perf_counter() - started) * 1000

    assert lag_ms < 5000
    print(f"replicated after ~{lag_ms:.0f} ms")


async def test_the_replica_refuses_writes(replica: AsyncEngine) -> None:
    async with replica.connect() as connection:
        with pytest.raises(DBAPIError, match="read-only transaction"):
            await connection.execute(text("INSERT INTO tags (name) VALUES ('nope')"))


async def test_the_replica_is_a_standby(replica: AsyncEngine) -> None:
    async with replica.connect() as connection:
        assert await connection.scalar(text("SELECT pg_is_in_recovery()")) is True
