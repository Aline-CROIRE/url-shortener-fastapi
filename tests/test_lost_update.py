"""The "lost update" problem (Module 5) shown with two REAL database connections.

Two visits happen at the same moment. Each one is its own transaction.
These tests commit for real, so they create their own rows and delete them after.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

pytestmark = pytest.mark.anyio

READ_COUNT = text("SELECT click_count FROM urls WHERE id = :id")
SET_COUNT = text("UPDATE urls SET click_count = :value WHERE id = :id")
ADD_ONE_IN_SQL = text("UPDATE urls SET click_count = click_count + 1 WHERE id = :id")


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().test_database_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
async def url_id(engine: AsyncEngine) -> AsyncIterator[int]:
    async with engine.begin() as connection:  # committed for real
        user_id = (
            await connection.execute(
                text(
                    "INSERT INTO users (email, username, hashed_password) "
                    "VALUES ('lost-update@test.local', 'lost-update', 'h') RETURNING id"
                )
            )
        ).scalar_one()
        new_url_id = (
            await connection.execute(
                text(
                    "INSERT INTO urls (original_url, short_code, owner_id) "
                    "VALUES ('https://x.com/', 'lostupd', :owner) RETURNING id"
                ),
                {"owner": user_id},
            )
        ).scalar_one()
    yield new_url_id
    async with engine.begin() as connection:  # clean up (CASCADE removes the URL)
        await connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})


async def final_count(engine: AsyncEngine, url_id: int) -> int:
    async with engine.connect() as connection:
        return (await connection.execute(READ_COUNT, {"id": url_id})).scalar_one()


async def test_adding_one_in_python_loses_an_update(engine: AsyncEngine, url_id: int) -> None:
    async with engine.connect() as visit_a, engine.connect() as visit_b:
        await visit_a.begin()
        await visit_b.begin()
        # Both visits READ the counter before either one writes
        seen_by_a = (await visit_a.execute(READ_COUNT, {"id": url_id})).scalar_one()
        seen_by_b = (await visit_b.execute(READ_COUNT, {"id": url_id})).scalar_one()
        assert seen_by_a == seen_by_b == 0
        # Each computes "+1" in Python and writes the result back
        await visit_a.execute(SET_COUNT, {"id": url_id, "value": seen_by_a + 1})
        await visit_a.commit()
        await visit_b.execute(SET_COUNT, {"id": url_id, "value": seen_by_b + 1})
        await visit_b.commit()

    assert await final_count(engine, url_id) == 1  # 2 visits, but only 1 counted!


async def test_adding_one_in_sql_counts_both_visits(engine: AsyncEngine, url_id: int) -> None:
    async with engine.connect() as visit_a, engine.connect() as visit_b:
        await visit_a.begin()
        await visit_b.begin()
        await visit_a.execute(ADD_ONE_IN_SQL, {"id": url_id})  # A now holds the row lock

        # B tries the same UPDATE: Postgres makes it WAIT for A's lock
        visit_b_update = asyncio.create_task(visit_b.execute(ADD_ONE_IN_SQL, {"id": url_id}))
        await asyncio.sleep(0.3)
        assert not visit_b_update.done()  # still waiting: proof of the row lock

        await visit_a.commit()  # A finishes -> lock released -> B continues
        await visit_b_update  # B re-reads the NEW value (1) and adds 1
        await visit_b.commit()

    assert await final_count(engine, url_id) == 2  # both visits counted
