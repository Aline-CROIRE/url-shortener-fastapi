"""The redirect cache against a REAL Redis server.

Start it first:  docker compose up -d redis
Uses database 15 (REDIS_TEST_URL) and deletes only its own keys.
If Redis is not reachable, every test here is skipped.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.cache import CachedRedirect, RedisRedirectCache

# 127.0.0.1, not "localhost": on Windows "localhost" tries IPv6 first (slow)
REDIS_TEST_URL = os.getenv("REDIS_TEST_URL", "redis://127.0.0.1:6379/15")


def redis_is_reachable() -> bool:
    async def ping() -> bool:
        client = Redis.from_url(REDIS_TEST_URL, socket_connect_timeout=0.5)
        try:
            return bool(await client.ping())
        except (RedisError, OSError):
            return False
        finally:
            await client.aclose()

    return asyncio.run(ping())


pytestmark = [
    pytest.mark.integration,
    pytest.mark.anyio,
    pytest.mark.skipif(not redis_is_reachable(), reason=f"Redis not reachable at {REDIS_TEST_URL}"),
]


@pytest.fixture
async def client() -> AsyncIterator[Redis]:
    redis = Redis.from_url(REDIS_TEST_URL, decode_responses=True)
    yield redis
    keys = [k async for k in redis.scan_iter("redirect:v1:itest*")]
    if keys:
        await redis.delete(*keys)
    await redis.aclose()


async def test_set_get_and_invalidate_on_real_redis(client: Redis) -> None:
    cache = RedisRedirectCache(client, ttl_seconds=900)
    now = datetime.now(UTC)
    entry = CachedRedirect(42, "https://www.python.org/", now + timedelta(minutes=5))

    await cache.set("itest1", entry, now)

    assert await cache.get("itest1") == entry
    assert 290 <= await client.ttl(cache.key("itest1")) <= 300  # capped by the 5-minute expiry
    await cache.invalidate("itest1")
    assert await cache.get("itest1") is None


async def test_redis_expires_the_key_by_itself(client: Redis) -> None:
    cache = RedisRedirectCache(client, ttl_seconds=1)

    await cache.set("itest2", CachedRedirect(1, "https://x.test/", None), datetime.now(UTC))
    assert await cache.get("itest2") is not None
    await asyncio.sleep(1.5)

    assert await cache.get("itest2") is None  # TTL ran out: Redis deleted it


async def test_ping(client: Redis) -> None:
    assert await RedisRedirectCache(client, 900).ping() is True
