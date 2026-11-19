"""Two updates of the same URL at the same moment take turns (SELECT ... FOR UPDATE).

Without the row lock both would start from the same old tags, and the result
could mix the two tag lists. With it, the second waits, then reads the first
one's result, and the last update wins cleanly.
"""

import asyncio

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.models import URL, Tag, User
from app.repositories import TagRepository, URLRepository, UserRepository
from app.schemas.url import URLUpdate
from app.services.urls import URLService

TAGS = ["zz-lock-old", "zz-lock-first", "zz-lock-second"]


def test_update_locks_the_url_row() -> None:
    statement = str(select(URL).with_for_update(of=URL).compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE OF urls" in statement  # what get_owned(for_update=True) adds


@pytest.mark.anyio
async def test_second_update_waits_and_wins_cleanly(test_settings: Settings) -> None:
    engine = create_async_engine(test_settings.test_database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        # A committed owner + URL tagged "old" (both sessions must really see it)
        async with sessions() as setup, setup.begin():
            owner = User(email="lock@example.com", username="lock", hashed_password="h")
            setup.add(
                URL(
                    original_url="https://example.com/",
                    short_code="lock01",
                    owner=owner,
                    tags=await TagRepository(setup).get_or_create_many(["zz-lock-old"]),
                )
            )
        owner_id = owner.id

        async with sessions() as first, sessions() as second:
            async with first.begin():
                # Update 1 holds the row lock and changes the tags (not committed yet)
                url = await URLRepository(first).get_owned("lock01", owner_id, for_update=True)
                assert url is not None
                url.tags = await TagRepository(first).get_or_create_many(["zz-lock-first"])
                await first.flush()

                service = URLService(
                    second, URLRepository(second), UserRepository(second), TagRepository(second)
                )
                update_2 = asyncio.create_task(
                    service.update("lock01", owner_id, URLUpdate(tags=["zz-lock-second"]))
                )
                await asyncio.sleep(0.3)
                assert not update_2.done()  # update 2 waits for the row lock
            # update 1 committed here: update 2 continues with fresh data
            await asyncio.wait_for(update_2, timeout=5)

        async with sessions() as check:
            final = await check.scalar(
                select(URL).where(URL.short_code == "lock01").options(selectinload(URL.tags))
            )
            assert final is not None
            assert [tag.name for tag in final.tags] == ["zz-lock-second"]  # no mix
    finally:
        async with engine.begin() as connection:  # these rows were really committed
            await connection.execute(delete(User).where(User.email == "lock@example.com"))
            await connection.execute(delete(Tag).where(func.lower(Tag.name).in_(TAGS)))
        await engine.dispose()
