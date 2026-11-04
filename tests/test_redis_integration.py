"""Integration tests against a REAL Redis server.

Start Redis first, for example:  docker compose up -d redis
The tests use database 15 by default (REDIS_TEST_URL) and only delete the keys they
create. If Redis is not reachable, every test in this file is skipped.
"""

import asyncio
import os
import secrets
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from redis import Redis as SyncRedis
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.main import create_app
from app.storage.base import UrlRecord
from app.storage.redis import KEY_PREFIX, RedisUrlRepository

# 127.0.0.1, not "localhost": on Windows "localhost" tries IPv6 (::1) first, which the
# Redis container port does not listen on, so every connection would be slow.
REDIS_TEST_URL = os.getenv("REDIS_TEST_URL", "redis://127.0.0.1:6379/15")

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def sync_redis() -> Iterator[SyncRedis]:
    client = SyncRedis.from_url(REDIS_TEST_URL, decode_responses=True, socket_connect_timeout=1)
    try:
        client.ping()
    except (RedisError, OSError):
        client.close()
        pytest.skip(f"Redis is not reachable at {REDIS_TEST_URL}")
    yield client
    client.close()


@pytest.fixture
def created_codes(sync_redis: SyncRedis) -> Iterator[list[str]]:
    # Each test adds its codes here; they are deleted afterwards
    codes: list[str] = []
    yield codes
    if codes:
        sync_redis.delete(*(f"{KEY_PREFIX}{code}" for code in codes))


@pytest.fixture
async def repository(sync_redis: SyncRedis) -> AsyncIterator[RedisUrlRepository]:
    client = Redis.from_url(REDIS_TEST_URL, decode_responses=True)
    yield RedisUrlRepository(client)
    await client.aclose()


def new_record(url: str = "https://example.com") -> UrlRecord:
    # "T" + 6 random chars: a valid 7-character code that will not clash with real data
    code = "T" + secrets.token_hex(3)
    return UrlRecord(short_code=code, original_url=url, created_at=datetime.now(timezone.utc))


@pytest.mark.anyio
async def test_save_and_get_roundtrip(
    repository: RedisUrlRepository, created_codes: list[str]
) -> None:
    record = new_record()
    created_codes.append(record.short_code)

    assert await repository.save_if_absent(record) is True
    assert await repository.get(record.short_code) == record


@pytest.mark.anyio
async def test_set_nx_refuses_existing_code(
    repository: RedisUrlRepository, created_codes: list[str]
) -> None:
    record = new_record("https://first.com")
    created_codes.append(record.short_code)
    await repository.save_if_absent(record)

    clash = UrlRecord(record.short_code, "https://second.com", record.created_at)

    assert await repository.save_if_absent(clash) is False
    assert (await repository.get(record.short_code)).original_url == "https://first.com"


@pytest.mark.anyio
async def test_set_nx_is_atomic_under_concurrency(
    repository: RedisUrlRepository, created_codes: list[str]
) -> None:
    # 20 requests try to save the SAME code at the same time: exactly one may win
    code = new_record().short_code
    created_codes.append(code)
    records = [
        UrlRecord(code, f"https://site{i}.com", datetime.now(timezone.utc)) for i in range(20)
    ]

    results = await asyncio.gather(*(repository.save_if_absent(r) for r in records))

    assert results.count(True) == 1


@pytest.mark.anyio
async def test_ping(repository: RedisUrlRepository) -> None:
    assert await repository.ping() is True


def test_full_api_flow_with_redis_backend(sync_redis: SyncRedis, created_codes: list[str]) -> None:
    settings = Settings(
        environment="testing",
        storage_backend="redis",
        redis_url=REDIS_TEST_URL,
        base_url="http://short.test",
    )
    with TestClient(create_app(settings), follow_redirects=False) as client:
        assert client.get("/health").json() == {"status": "healthy", "storage": "redis"}

        created = client.post("/api/v1/urls", json={"original_url": "https://www.python.org/"})
        assert created.status_code == 201
        code = created.json()["short_code"]
        created_codes.append(code)

        # The data really is in Redis
        assert sync_redis.exists(f"{KEY_PREFIX}{code}") == 1

        redirect = client.get(f"/{code}")
        assert redirect.status_code == 302
        assert redirect.headers["location"] == "https://www.python.org/"
