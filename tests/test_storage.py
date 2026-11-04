from datetime import datetime, timezone

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.exceptions import StorageUnavailableError
from app.storage.base import UrlRecord, UrlRepository
from app.storage.memory import InMemoryUrlRepository
from app.storage.redis import RedisUrlRepository

pytestmark = pytest.mark.anyio


class FakeRedis:
    """Tiny stand-in for redis.asyncio.Redis, enough for SET NX / GET / PING."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def set(self, key: str, value: str, nx: bool = False) -> bool | None:
        if nx and key in self.data:
            return None  # real Redis returns None when NX blocks the write
        self.data[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def ping(self) -> bool:
        return True


class BrokenRedis:
    async def set(self, *args, **kwargs):
        raise RedisConnectionError("connection refused")

    async def get(self, *args, **kwargs):
        raise RedisConnectionError("connection refused")

    async def ping(self):
        raise RedisConnectionError("connection refused")


def make_record(code: str = "abc1234", url: str = "https://example.com") -> UrlRecord:
    return UrlRecord(short_code=code, original_url=url, created_at=datetime.now(timezone.utc))


@pytest.fixture(params=["memory", "redis"])
def repository(request: pytest.FixtureRequest) -> UrlRepository:
    # Every test below runs twice: once per repository implementation
    if request.param == "memory":
        return InMemoryUrlRepository()
    return RedisUrlRepository(FakeRedis())


async def test_save_and_get(repository: UrlRepository) -> None:
    record = make_record()

    assert await repository.save_if_absent(record) is True
    assert await repository.get(record.short_code) == record


async def test_save_if_absent_refuses_existing_code(repository: UrlRepository) -> None:
    await repository.save_if_absent(make_record(url="https://first.com"))

    assert await repository.save_if_absent(make_record(url="https://second.com")) is False
    assert (await repository.get("abc1234")).original_url == "https://first.com"


async def test_get_missing_returns_none(repository: UrlRepository) -> None:
    assert await repository.get("missing") is None


async def test_ping(repository: UrlRepository) -> None:
    assert await repository.ping() is True


async def test_redis_keys_use_prefix() -> None:
    fake = FakeRedis()
    await RedisUrlRepository(fake).save_if_absent(make_record())
    assert list(fake.data) == ["url:abc1234"]


async def test_redis_down_raises_storage_unavailable() -> None:
    repository = RedisUrlRepository(BrokenRedis())

    with pytest.raises(StorageUnavailableError):
        await repository.get("abc1234")
    with pytest.raises(StorageUnavailableError):
        await repository.save_if_absent(make_record())
    assert await repository.ping() is False
