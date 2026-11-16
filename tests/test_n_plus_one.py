"""N+1 queries: show the problem, prove the fix, and keep it fixed.

The query counter listens to SQLAlchemy's "before_cursor_execute" event and counts
every SQL statement (transaction bookkeeping like SAVEPOINT is not counted).
"""

import re
from collections.abc import Callable
from itertools import count
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, lazyload

from app.models import URL, Click, Tag, User
from app.repositories.url import WITH_OWNER_AND_TAGS

RunDb = Callable[..., Any]
_numbers = count(1)


def seed_urls(run_db: RunDb, n: int, owner_id: int | None = None) -> list[str]:
    """n URLs with 2 tags each. Without owner_id, every URL gets its OWN new owner."""
    batch = next(_numbers)

    async def work(session: AsyncSession) -> list[str]:
        tags = [Tag(name=f"B{batch}a"), Tag(name=f"B{batch}b")]
        codes = []
        for i in range(n):
            code = f"b{batch}u{i}"
            if owner_id is None:
                owner = User(email=f"{code}@x.test", username=code, hashed_password="!")
                session.add(
                    URL(original_url="https://x.test/", short_code=code, owner=owner, tags=tags)
                )
            else:
                session.add(
                    URL(
                        original_url="https://x.test/",
                        short_code=code,
                        owner_id=owner_id,
                        tags=tags,
                    )
                )
            codes.append(code)
        return codes

    return run_db(work)


# ---------- 1. the problem: lazy loading = 1 + 2N queries ----------


@pytest.mark.parametrize("n", [3, 10])
def test_lazy_loading_causes_n_plus_one(run_db: RunDb, query_counter: Callable, n: int) -> None:
    codes = seed_urls(run_db, n)

    async def list_lazily(session: AsyncSession) -> None:
        # lazyload(): opt back in to lazy loading for THIS query only (our models
        # use lazy="raise"), so we can watch the problem happen
        statement = (
            select(URL)
            .where(URL.short_code.in_(codes))
            .options(lazyload(URL.owner), lazyload(URL.tags))
        )
        for url in (await session.scalars(statement)).all():
            await url.awaitable_attrs.owner  # one query per URL...
            await url.awaitable_attrs.tags  # ...and another one per URL

    with query_counter() as counter:
        run_db(list_lazily)

    assert counter.count == 1 + 2 * n  # grows with every URL: 7 for 3, 21 for 10


# ---------- 2. the fix: eager loading = 2 queries for any N ----------


@pytest.mark.parametrize("n", [3, 10])
def test_eager_loading_uses_two_queries_for_any_size(
    run_db: RunDb, query_counter: Callable, n: int
) -> None:
    codes = seed_urls(run_db, n)

    async def list_eagerly(session: AsyncSession) -> None:
        statement = select(URL).where(URL.short_code.in_(codes)).options(*WITH_OWNER_AND_TAGS)
        for url in (await session.scalars(statement)).all():
            _ = (url.owner.username, [tag.name for tag in url.tags])  # no query: already loaded

    with query_counter() as counter:
        run_db(list_eagerly)

    assert counter.count == 2, counter.statements  # URLs + owners (JOIN), then all tags (IN)


# ---------- 3. the endpoints stay cheap however much data there is ----------


def test_url_list_uses_at_most_3_queries_regardless_of_size(
    db_client: TestClient, owner_id: int, run_db: RunDb, query_counter: Callable
) -> None:
    seed_urls(run_db, 3, owner_id)
    with query_counter() as small:
        assert len(db_client.get("/api/v1/urls").json()["items"]) == 3

    seed_urls(run_db, 27, owner_id)
    with query_counter() as large:
        assert len(db_client.get("/api/v1/urls", params={"limit": 30}).json()["items"]) == 30

    assert small.count <= 3, small.statements
    assert large.count == small.count  # 10x the URLs, the same number of queries


def test_get_one_url_uses_at_most_2_queries(
    db_client: TestClient, owner_id: int, run_db: RunDb, query_counter: Callable
) -> None:
    code = seed_urls(run_db, 1, owner_id)[0]

    with query_counter() as counter:
        assert db_client.get(f"/api/v1/urls/{code}").status_code == 200

    assert counter.count <= 2, counter.statements


def test_redirect_uses_3_statements(
    db_client: TestClient, owner_id: int, run_db: RunDb, query_counter: Callable
) -> None:
    code = seed_urls(run_db, 1, owner_id)[0]

    with query_counter() as counter:
        assert db_client.get(f"/{code}").status_code == 302

    # SELECT the URL, INSERT the click, UPDATE click_count (+1 in SQL)
    assert counter.count == 3, counter.statements


def test_analytics_query_count_does_not_grow_with_clicks(
    db_client: TestClient, owner_id: int, run_db: RunDb, query_counter: Callable
) -> None:
    code = seed_urls(run_db, 1, owner_id)[0]

    def add_clicks(n: int) -> None:
        async def work(session: AsyncSession) -> None:
            url_id = await session.scalar(select(URL.id).where(URL.short_code == code))
            session.add_all(Click(url_id=url_id, country="RW") for _ in range(n))

        run_db(work)

    add_clicks(3)
    with query_counter() as few:
        db_client.get(f"/api/v1/analytics/{code}")
    add_clicks(300)
    with query_counter() as many:
        db_client.get(f"/api/v1/analytics/{code}")

    assert few.count == many.count == 6  # owner check + 5 aggregation queries


# ---------- 4. the alarm: lazy="raise" ----------


def test_touching_an_unloaded_relationship_raises_a_clear_error(run_db: RunDb) -> None:
    code = seed_urls(run_db, 1)[0]

    async def forget_to_load(session: AsyncSession) -> None:
        url = await session.scalar(select(URL).where(URL.short_code == code))  # no options
        _ = url.tags

    with pytest.raises(
        InvalidRequestError, match=re.escape("'URL.tags' is not available due to lazy='raise'")
    ):
        run_db(forget_to_load)


# ---------- 5. why tags use selectinload, not joinedload ----------


def test_joinedload_on_a_collection_repeats_rows(run_db: RunDb) -> None:
    codes = seed_urls(run_db, 2)  # 2 URLs, 2 tags each

    async def raw_row_count(session: AsyncSession) -> int:
        statement = select(URL).where(URL.short_code.in_(codes)).options(joinedload(URL.tags))
        connection = await session.connection()
        return len((await connection.execute(statement)).all())  # rows as Postgres sends them

    assert run_db(raw_row_count) == 4  # 2 URLs x 2 tags: each URL sent twice
