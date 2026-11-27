"""Module 8 cache rules: key name, what is stored, what is never cached, fail open.

The cache-aside basics (hit, miss, TTL, invalidation, the tombstone race) are in
tests/test_redirect_cache.py; this file adds the Module 8 spec's extra rules.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import update

from app.cache import RedisRedirectCache
from app.models import URL
from tests.test_redirect_cache import API, FakeRedis, create, fake_redis, key

__all__ = ["fake_redis"]  # the fixture, imported so pytest finds it in this file


def test_key_is_named_after_what_it_holds() -> None:
    # "url:redirect:<code>": what (a URL), for which use (the redirect), which one
    assert RedisRedirectCache.key("abc1234") == "url:redirect:abc1234"


def test_cache_stores_only_what_the_redirect_needs(
    db_client: TestClient, owner_id: int, fake_redis: FakeRedis
) -> None:
    code = create(db_client, tags=["Work"])
    db_client.put(f"{API}/{code}", json={"title": "Private title", "description": "Notes"})
    fake_redis.data.pop(key(code))  # the tombstone of that change has ended

    db_client.get(f"/{code}")

    stored = json.loads(fake_redis.data[key(code)])
    # No owner, title, description or click_count: smaller, and no data that
    # would need invalidating when it changes
    assert set(stored) == {"url_id", "original_url", "expires_at"}


def expire_in_db(code: str) -> Callable[[Any], Any]:
    async def work(session: Any) -> None:
        past = datetime.now(UTC) - timedelta(minutes=1)
        await session.execute(update(URL).where(URL.short_code == code).values(expires_at=past))

    return work


@pytest.mark.parametrize("make_dead", ["switched_off", "deleted", "expired"])
def test_dead_links_are_never_cached(
    db_client: TestClient,
    owner_id: int,
    fake_redis: FakeRedis,
    run_db: Callable,
    make_dead: str,
) -> None:
    code = create(db_client)
    if make_dead == "switched_off":
        db_client.put(f"{API}/{code}", json={"is_active": False})
    elif make_dead == "deleted":
        db_client.delete(f"{API}/{code}")
    else:
        run_db(expire_in_db(code))
    fake_redis.data.pop(key(code), None)  # the tombstone has ended: the cache is empty

    for _ in range(2):  # the second visit must not find anything cached either
        assert db_client.get(f"/{code}").status_code == 410

    assert key(code) not in fake_redis.data


def test_cache_hit_still_answers_302_so_every_click_reaches_us(
    db_client: TestClient, owner_id: int, fake_redis: FakeRedis
) -> None:
    code = create(db_client)
    db_client.get(f"/{code}")  # cached now

    response = db_client.get(f"/{code}")

    # 301 = "moved forever": browsers would remember it and never ask us again,
    # so later clicks would be lost for the analytics
    assert response.status_code == 302
    assert "max-age" not in response.headers.get("cache-control", "")


def test_slow_redis_counts_as_a_miss(
    db_client: TestClient,
    owner_id: int,
    fake_redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    code = create(db_client)

    async def timeout(*args: Any, **kwargs: Any) -> Any:
        raise RedisTimeoutError("Timeout reading from socket")

    monkeypatch.setattr(fake_redis, "get", timeout)
    monkeypatch.setattr(fake_redis, "set", timeout)

    response = db_client.get(f"/{code}")

    assert response.status_code == 302  # the database answered
    assert "Redis unavailable" in caplog.text


def test_redis_down_never_blocks_a_change_or_a_delete(
    db_client: TestClient,
    owner_id: int,
    fake_redis: FakeRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    code = create(db_client)
    fake_redis.down = True

    assert db_client.put(f"{API}/{code}", json={"is_active": False}).status_code == 200
    assert db_client.delete(f"{API}/{code}").status_code == 204
    # Logged as an ERROR: the old entry may live until its TTL ends
    assert "Could not invalidate cached redirect" in caplog.text
