"""New tags are always inserted in one fixed (sorted) order, so two requests that
save the same new tags in a different order cannot deadlock each other."""

import asyncio

import pytest
from sqlalchemy import delete, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.models import Tag
from app.repositories import TagRepository


@pytest.mark.anyio
async def test_new_tags_are_inserted_in_sorted_order(db_session: AsyncSession) -> None:
    asked = ["zz-web", "ZZ-Python", "zz-api"]

    tags = await TagRepository(db_session).get_or_create_many(asked)

    assert [tag.name for tag in tags] == asked  # returned in the order asked
    # ids grow in insert order: sorted (lower-case) order, whatever order was asked
    by_id = sorted(tags, key=lambda tag: tag.id)
    assert [tag.name for tag in by_id] == ["zz-api", "ZZ-Python", "zz-web"]


@pytest.mark.anyio
async def test_two_requests_with_opposite_orders_both_succeed(test_settings: Settings) -> None:
    """T1 saves new tags without committing; T2 asks for them in the other order.

    T2 waits for T1 (no deadlock), then finds T1's tags instead of creating copies.
    """
    names = ["zz-deadlock-web", "zz-deadlock-python"]
    engine = create_async_engine(test_settings.test_database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions() as first, sessions() as second:
            first_tags = await TagRepository(first).get_or_create_many(names)  # not committed

            waiting = asyncio.create_task(
                TagRepository(second).get_or_create_many(list(reversed(names)))
            )
            await asyncio.sleep(0.3)
            assert not waiting.done()  # T2 waits for T1's uncommitted tags

            await first.commit()
            second_tags = await asyncio.wait_for(waiting, timeout=5)
            await second.commit()

        assert sorted(tag.id for tag in second_tags) == sorted(tag.id for tag in first_tags)
    finally:
        async with engine.begin() as connection:  # these rows were really committed
            await connection.execute(delete(Tag).where(func.lower(Tag.name).in_(names)))
        await engine.dispose()
