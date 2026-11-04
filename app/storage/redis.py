import json
from datetime import datetime

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.exceptions import StorageUnavailableError
from app.storage.base import UrlRecord

KEY_PREFIX = "url:"


class RedisUrlRepository:
    """Stores each short URL as a JSON string under the key "url:<short_code>"."""

    def __init__(self, client: Redis) -> None:
        self._client = client

    @staticmethod
    def _key(short_code: str) -> str:
        return f"{KEY_PREFIX}{short_code}"

    async def save_if_absent(self, record: UrlRecord) -> bool:
        value = json.dumps(
            {
                "short_code": record.short_code,
                "original_url": record.original_url,
                "created_at": record.created_at.isoformat(),
            }
        )
        # SET ... NX = only set if the key does Not eXist. Redis does the check
        # and the write in one atomic step, so two codes can never collide.
        try:
            was_set = await self._client.set(self._key(record.short_code), value, nx=True)
        except (RedisError, OSError) as exc:
            raise StorageUnavailableError() from exc
        return bool(was_set)

    async def get(self, short_code: str) -> UrlRecord | None:
        try:
            value = await self._client.get(self._key(short_code))
        except (RedisError, OSError) as exc:
            raise StorageUnavailableError() from exc
        if value is None:
            return None
        data = json.loads(value)
        return UrlRecord(
            short_code=data["short_code"],
            original_url=data["original_url"],
            created_at=datetime.fromisoformat(data["created_at"]),
        )

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except (RedisError, OSError):
            return False
