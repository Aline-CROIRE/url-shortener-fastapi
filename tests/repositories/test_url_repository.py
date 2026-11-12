from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import DbSession, URLRepoDep
from app.models import URL, User
from app.repositories import URLRepository

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)  # a frozen clock for the tests
ONE_MICROSECOND = timedelta(microseconds=1)


@pytest.fixture
def repo(db_session: AsyncSession) -> URLRepository:
    return URLRepository(db_session)


async def codes(repo: URLRepository, statement) -> set[str]:
    return {url.short_code for url in await repo.fetch_all(statement)}


# ---------- active_urls() / expired_urls() ----------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("is_active", "expires_at", "active", "expired"),
    [
        (True, None, True, False),  # never expires
        (True, NOW + timedelta(days=1), True, False),  # expires tomorrow
        (True, NOW + ONE_MICROSECOND, True, False),  # still valid, but only just
        (True, NOW, False, True),  # JUST expired: exactly now -> expired, not active
        (True, NOW - timedelta(days=1), False, True),  # expired yesterday
        (False, None, False, False),  # switched off, no expiry
        (False, NOW + timedelta(days=1), False, False),  # switched off, future expiry
        (False, NOW - timedelta(days=1), False, True),  # switched off AND expired
    ],
)
async def test_active_and_expired_rules(
    repo: URLRepository,
    make_url,
    is_active: bool,
    expires_at: datetime | None,
    active: bool,
    expired: bool,
) -> None:
    url = await make_url(is_active=is_active, expires_at=expires_at)

    assert (url.short_code in await codes(repo, repo.active_urls(now=NOW))) is active
    assert (url.short_code in await codes(repo, repo.expired_urls(now=NOW))) is expired


@pytest.mark.anyio
async def test_no_switched_on_url_falls_between_active_and_expired(
    repo: URLRepository, make_url
) -> None:
    for expires_at in (NOW - ONE_MICROSECOND, NOW, NOW + ONE_MICROSECOND):
        await make_url(expires_at=expires_at)

    active = await codes(repo, repo.active_urls(now=NOW))
    expired = await codes(repo, repo.expired_urls(now=NOW))

    assert active.isdisjoint(expired)  # never in both lists
    assert len(active | expired) == 3  # never in neither list


@pytest.mark.anyio
async def test_active_urls_uses_the_current_time_by_default(repo: URLRepository, make_url) -> None:
    url = await make_url(expires_at=datetime.now(UTC) - timedelta(seconds=1))
    assert url.short_code not in await codes(repo, repo.active_urls())


# ---------- popular_urls() ----------


@pytest.mark.anyio
async def test_popular_urls_orders_by_clicks_with_id_tie_breaker(
    repo: URLRepository, make_url
) -> None:
    low = await make_url(click_count=1)
    tie_first = await make_url(click_count=5)
    tie_second = await make_url(click_count=5)
    top = await make_url(click_count=9)

    result = await repo.fetch_all(repo.popular_urls(limit=10))

    assert [u.id for u in result] == [top.id, tie_first.id, tie_second.id, low.id]


@pytest.mark.anyio
async def test_popular_urls_respects_the_limit(repo: URLRepository, make_url) -> None:
    for clicks in range(5):
        await make_url(click_count=clicks)

    result = await repo.fetch_all(repo.popular_urls(limit=2))

    assert [u.click_count for u in result] == [4, 3]


@pytest.mark.parametrize("limit", [0, -1, 101])
def test_popular_urls_rejects_bad_limits(limit: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 100"):
        URLRepository.popular_urls(limit=limit)


# ---------- composing ----------


@pytest.mark.anyio
async def test_statements_can_be_composed(
    repo: URLRepository, make_url, owner: User, db_session: AsyncSession
) -> None:
    other = User(email="other@example.com", username="other", hashed_password="h")
    mine = await make_url()
    await make_url(owner=other)  # active, but not mine
    await make_url(is_active=False)  # mine, but not active

    statement = repo.active_urls(now=NOW).where(URL.owner_id == owner.id)

    assert await codes(repo, statement) == {mine.short_code}


@pytest.mark.anyio
async def test_popular_active_urls(repo: URLRepository, make_url) -> None:
    await make_url(click_count=100, is_active=False)  # most clicked, but switched off
    best_active = await make_url(click_count=50)

    statement = repo.popular_urls(limit=1).where(URL.is_active.is_(True))

    assert (await repo.fetch_first(statement)).id == best_active.id


# ---------- single URLs ----------


@pytest.mark.anyio
async def test_add_and_get(repo: URLRepository, owner: User) -> None:
    url = await repo.add(URL(original_url="https://x.com/", short_code="added1", owner=owner))

    assert url.id is not None  # flush() filled the id
    assert await repo.get(url.id) is url
    assert await repo.get(999_999) is None


@pytest.mark.anyio
async def test_get_by_short_code_and_exists(repo: URLRepository, make_url) -> None:
    url = await make_url(short_code="findMe")

    assert await repo.get_by_short_code("findMe") is url
    assert await repo.get_by_short_code("nope") is None
    assert await repo.short_code_exists("findMe") is True
    assert await repo.short_code_exists("nope") is False


@pytest.mark.anyio
async def test_fetch_first_returns_none_when_empty(repo: URLRepository) -> None:
    assert await repo.fetch_first(repo.active_urls(now=NOW)) is None


# ---------- dependency injection ----------


def test_route_gets_a_repository_bound_to_the_request_session(
    app: FastAPI, db_client: TestClient
) -> None:
    @app.get("/_test/repo")
    async def check(repo: URLRepoDep, db: DbSession) -> dict[str, bool]:
        # FastAPI calls get_db only ONCE per request and shares the result,
        # so the repository uses the very same session as the route
        return {"same_session": repo._session is db}

    assert db_client.get("/_test/repo").json() == {"same_session": True}
