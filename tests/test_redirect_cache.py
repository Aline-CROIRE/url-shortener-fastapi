"""Cache-aside for redirects, tested with a small in-memory FakeRedis."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import get_redirect_cache
from app.cache import CachedRedirect, NullRedirectCache, RedisRedirectCache
from app.core.config import Settings
from app.main import create_app
from app.repositories import ClickRepository

API = "/api/v1/urls"
TARGET = "https://www.python.org/"
TTL = 900


class FakeRedis:
    """Just enough of redis.asyncio.Redis: GET, SET with EX, DELETE, PING."""

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

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self._check()
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
    db_client: TestClient, owner_id: int, fake_redis: FakeRedis, query_counter: Callable
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

    assert miss.count == 3  # SELECT + INSERT click + UPDATE count
    assert hit.count == 2, hit.statements  # no SELECT: the cache answered
    assert db_client.get(f"{API}/{code}").json()["click_count"] == 2  # both visits recorded


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
    assert key(code) not in fake_redis.data  # the expired entry was removed


# ---------- invalidation ----------


def test_put_invalidates_so_the_new_target_is_used(
    db_client: TestClient, owner_id: int, fake_redis: FakeRedis
) -> None:
    code = create(db_client)
    db_client.get(f"/{code}")  # cached with the old target

    db_client.put(f"{API}/{code}", json={"original_url": "https://fastapi.tiangolo.com/"})

    assert key(code) not in fake_redis.data
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


def test_stale_entry_for_a_url_that_no_longer_exists_returns_404(
    db_client: TestClient, owner_id: int, fake_redis: FakeRedis
) -> None:
    # e.g. the owner was deleted: the row is gone but the cache still remembers it
    fake_redis.data[key("ghost1")] = CachedRedirect(999_999, TARGET, None).to_json()

    response = db_client.get("/ghost1")

    assert response.status_code == 404  # the click INSERT failed its foreign key -> DB path
    assert key("ghost1") not in fake_redis.data


def test_other_database_errors_on_a_cache_hit_are_not_hidden(
    db_client: TestClient, owner_id: int, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = create(db_client)
    db_client.get(f"/{code}")  # cached

    async def broken_add(self: ClickRepository, click: Any) -> Any:
        raise IntegrityError("INSERT INTO clicks ...", {}, Exception("check constraint failed"))

    monkeypatch.setattr(ClickRepository, "add", broken_add)

    assert db_client.get(f"/{code}").status_code == 500


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
    assert health.status_code == 200  # still healthy: only slower
    assert health.json()["cache"] == "down"


def test_broken_cache_entry_is_treated_as_a_miss(
    db_client: TestClient, owner_id: int, fake_redis: FakeRedis
) -> None:
    code = create(db_client)
    fake_redis.data[key(code)] = "{not valid json"

    assert db_client.get(f"/{code}").status_code == 302
    assert CachedRedirect.from_json(fake_redis.data[key(code)]).original_url == TARGET  # repaired


def test_health_shows_the_cache_is_ok(db_client: TestClient, fake_redis: FakeRedis) -> None:
    assert db_client.get("/health").json()["cache"] == "ok"


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
