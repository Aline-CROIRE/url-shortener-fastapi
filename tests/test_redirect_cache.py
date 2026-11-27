"""Cache-aside for redirects, tested with a small in-memory FakeRedis."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from app.api.dependencies import get_redirect_cache
from app.cache import CachedRedirect, NullRedirectCache, RedisRedirectCache
from app.cache.redirect_cache import TOMBSTONE, TOMBSTONE_SECONDS
from app.core.config import Settings
from app.main import create_app
from app.repositories import URLRepository
from app.services.clicks import ClickEvent

API = "/api/v1/urls"
TARGET = "https://www.python.org/"
TTL = 900


class FakeRedis:
    """Just enough of redis.asyncio.Redis: GET, SET with EX and NX, DELETE, PING."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.down = False  # True = every call fails like a dead Redis

    def _check(self) -> None:
        if self.down:
            raise RedisConnectionError("Connection refused")

    async def get(self, key: str) -> str | None:
        self._check()
        return self.data.get(key)

    async def set(
        self, key: str, value: str, ex: int | None = None, nx: bool = False
    ) -> bool | None:
        self._check()
        if nx and key in self.data:
            return None  # like Redis: NX and the key exists -> nothing written
        self.data[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def delete(self, key: str) -> int:
        self._check()
        self.ttls.pop(key, None)
        return 1 if self.data.pop(key, None) is not None else 0

    async def ping(self) -> bool:
        self._check()
        return True


@pytest.fixture
def fake_redis(app: FastAPI) -> FakeRedis:
    fake = FakeRedis()
    app.dependency_overrides[get_redirect_cache] = lambda: RedisRedirectCache(fake, TTL)
    return fake


def key(code: str) -> str:
    return RedisRedirectCache.key(code)


def create(client: TestClient, **body: Any) -> str:
    response = client.post(API, json={"original_url": TARGET, **body})
    assert response.status_code == 201, response.text
    return response.json()["short_code"]


# ---------- hit and miss ----------


def test_first_visit_fills_the_cache_second_visit_skips_the_select(
    db_client: TestClient,
    owner_id: int,
    fake_redis: FakeRedis,
    query_counter: Callable,
    recorded_clicks: list[ClickEvent],
) -> None:
    code = create(db_client)
    assert key(code) not in fake_redis.data

    with query_counter() as miss:
        assert db_client.get(f"/{code}").status_code == 302
    cached = CachedRedirect.from_json(fake_redis.data[key(code)])
    assert cached.original_url == TARGET
    assert fake_redis.ttls[key(code)] == TTL  # 15 minutes

    with query_counter() as hit:
        response = db_client.get(f"/{code}")
    assert response.status_code == 302
    assert response.headers["location"] == TARGET

    assert miss.count == 1, miss.statements  # only the SELECT (the worker saves the click)
    assert hit.count == 0, hit.statements  # no database at all: the cache answered
    assert len(recorded_clicks) == 2  # both visits handed over to be counted


def test_ttl_never_outlives_the_link(
    db_client: TestClient, owner_id: int, fake_redis: FakeRedis
) -> None:
    expires = (datetime.now(UTC) + timedelta(minutes=2)).isoformat()
    code = create(db_client, expires_at=expires)

    db_client.get(f"/{code}")

    assert 100 < fake_redis.ttls[key(code)] <= 120  # 2 minutes, not 15


def test_link_that_expired_while_cached_returns_410(
    db_client: TestClient, owner_id: int, fake_redis: FakeRedis, run_db: Callable
) -> None:
    from sqlalchemy import update

    from app.models import URL

    code = create(db_client)
    db_client.get(f"/{code}")  # now cached
    past = datetime.now(UTC) - timedelta(seconds=1)
    entry = CachedRedirect.from_json(fake_redis.data[key(code)])
    fake_redis.data[key(code)] = CachedRedirect(entry.url_id, entry.original_url, past).to_json()

    async def expire(session: Any) -> None:
        await session.execute(update(URL).where(URL.short_code == code).values(expires_at=past))

    run_db(expire)

    response = db_client.get(f"/{code}")

    assert response.status_code == 410
    assert response.json()["error"]["code"] == "url_expired"
    assert fake_redis.data[key(code)] == TOMBSTONE  # the expired entry was replaced


# ---------- invalidation ----------


def test_put_invalidates_so_the_new_target_is_used(
    db_client: TestClient, owner_id: int, fake_redis: FakeRedis
) -> None:
    code = create(db_client)
    db_client.get(f"/{code}")  # cached with the old target

    db_client.put(f"{API}/{code}", json={"original_url": "https://fastapi.tiangolo.com/"})

    assert fake_redis.data[key(code)] == TOMBSTONE
    assert fake_redis.ttls[key(code)] == TOMBSTONE_SECONDS
    assert db_client.get(f"/{code}").headers["location"] == "https://fastapi.tiangolo.com/"


def test_switching_off_invalidates(
    db_client: TestClient, owner_id: int, fake_redis: FakeRedis
) -> None:
    code = create(db_client)
    db_client.get(f"/{code}")

    db_client.put(f"{API}/{code}", json={"is_active": False})

    assert db_client.get(f"/{code}").status_code == 410  # not served from a stale cache


def test_delete_invalidates(db_client: TestClient, owner_id: int, fake_redis: FakeRedis) -> None:
    code = create(db_client)
    db_client.get(f"/{code}")

    db_client.delete(f"{API}/{code}")

    response = db_client.get(f"/{code}")
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "url_deleted"


def test_entry_for_a_row_removed_outside_the_api_lives_until_its_ttl(
    db_client: TestClient, owner_id: int, fake_redis: FakeRedis, recorded_clicks: list[ClickEvent]
) -> None:
    # Known limit (Module 8): the API never removes URL rows (delete is soft), but a
    # row removed by hand in the database stays cached until its TTL ends (max 15
    # min). The hit needs no database, so nothing notices; the worker then drops
    # the click (tests/test_click_task.py).
    fake_redis.data[key("ghost1")] = CachedRedirect(999_999, TARGET, None).to_json()

    response = db_client.get("/ghost1")

    assert response.status_code == 302
    assert recorded_clicks[0].url_id == 999_999


def test_cache_hit_works_even_when_the_database_is_down(
    db_client: TestClient,
    owner_id: int,
    fake_redis: FakeRedis,
    recorded_clicks: list[ClickEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = create(db_client)
    db_client.get(f"/{code}")  # cached

    async def database_down(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionRefusedError("PostgreSQL is down")

    monkeypatch.setattr(URLRepository, "get_by_short_code", database_down)

    response = db_client.get(f"/{code}")

    assert response.status_code == 302  # popular links keep working during a DB outage
    assert len(recorded_clicks) == 2


# ---------- fail open: Redis problems never break redirects ----------


def test_redis_down_redirects_still_work(
    db_client: TestClient,
    owner_id: int,
    fake_redis: FakeRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    code = create(db_client)
    fake_redis.down = True

    response = db_client.get(f"/{code}")

    assert response.status_code == 302  # served from the database
    assert "Redis unavailable" in caplog.text
    assert (
        db_client.put(f"{API}/{code}", json={"title": "x"}).status_code == 200
    )  # invalidate fails quietly
    health = db_client.get("/health")
    # Redirects still work, but the app is NOT ready: Redis is also the click queue
    # (Module 8; in Module 6 the cache alone did not count)
    assert health.status_code == 503
    assert health.json()["cache"] == "down"


def test_broken_cache_entry_is_treated_as_a_miss(
    db_client: TestClient, owner_id: int, fake_redis: FakeRedis
) -> None:
    code = create(db_client)
    fake_redis.data[key(code)] = "{not valid json"

    assert db_client.get(f"/{code}").status_code == 302  # removed, then cached again
    assert CachedRedirect.from_json(fake_redis.data[key(code)]).original_url == TARGET  # repaired


@pytest.mark.anyio
async def test_broken_entry_stays_if_redis_fails_while_removing_it() -> None:
    fake = FakeRedis()
    fake.data[key("bad1")] = "{not valid json"

    async def failing_delete(key: str) -> int:
        raise RedisConnectionError("Connection refused")

    fake.delete = failing_delete  # type: ignore[method-assign]

    assert await RedisRedirectCache(fake, TTL).get("bad1") is None  # still just a miss


def test_health_shows_the_cache_is_ok(db_client: TestClient, fake_redis: FakeRedis) -> None:
    assert db_client.get("/health").json()["cache"] == "ok"


# ---------- the invalidation race (a late write after a change) ----------


@pytest.mark.anyio
async def test_late_write_after_invalidate_is_refused() -> None:
    """The timeline from the review, step by step.

    1. visitor: cache miss, reads the database: the link is live
    2. owner:   deletes the link, commits, invalidates the cache
    3. visitor: writes what it read in step 1 (now stale) to the cache
    Without protection the deleted link would redirect for 15 minutes.
    """
    fake = FakeRedis()
    cache = RedisRedirectCache(fake, TTL)
    now = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
    read_before_delete = CachedRedirect(1, TARGET, None)  # step 1

    await cache.invalidate("race1")  # step 2
    await cache.set("race1", read_before_delete, now)  # step 3: refused (NX)

    assert await cache.get("race1") is None  # the database will answer: 410
    assert fake.data[key("race1")] == TOMBSTONE


@pytest.mark.anyio
async def test_invalidate_overwrites_an_entry_written_just_before() -> None:
    fake = FakeRedis()
    cache = RedisRedirectCache(fake, TTL)
    now = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)

    await cache.set("race2", CachedRedirect(1, TARGET, None), now)  # visitor first
    await cache.invalidate("race2")  # then the owner's change

    assert await cache.get("race2") is None


def test_link_is_cached_again_after_the_tombstone_ends(
    db_client: TestClient, owner_id: int, fake_redis: FakeRedis
) -> None:
    code = create(db_client)
    db_client.put(f"{API}/{code}", json={"title": "new"})  # tombstone written
    del fake_redis.data[key(code)]  # Redis removes it by itself after 30 seconds

    db_client.get(f"/{code}")

    assert CachedRedirect.from_json(fake_redis.data[key(code)]).original_url == TARGET


# ---------- small units ----------


def test_ttl_for() -> None:
    cache = RedisRedirectCache(FakeRedis(), TTL)
    now = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)

    assert cache.ttl_for(CachedRedirect(1, TARGET, None), now) == TTL
    assert cache.ttl_for(CachedRedirect(1, TARGET, now + timedelta(seconds=60)), now) == 60


@pytest.mark.anyio
async def test_almost_expired_links_are_not_cached() -> None:
    fake = FakeRedis()
    now = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)

    await RedisRedirectCache(fake, TTL).set("soon1", CachedRedirect(1, TARGET, now), now)

    assert fake.data == {}


@pytest.mark.anyio
async def test_null_cache_does_nothing() -> None:
    cache = NullRedirectCache()
    entry = CachedRedirect(1, TARGET, None)

    await cache.set("a1b2c3", entry, datetime.now(UTC))
    await cache.invalidate("a1b2c3")

    assert await cache.get("a1b2c3") is None
    assert await cache.ping() is None


def test_lifespan_creates_a_redis_cache_when_configured(test_settings: Settings) -> None:
    settings = test_settings.model_copy(update={"redis_url": "redis://127.0.0.1:6379/15"})
    app = create_app(settings)

    with TestClient(app):  # runs the lifespan (Redis connects lazily, on first use)
        assert isinstance(app.state.redirect_cache, RedisRedirectCache)
