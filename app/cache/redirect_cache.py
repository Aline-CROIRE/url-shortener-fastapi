"""Cache-aside for redirects: a fast "notepad" in Redis in front of the database.

Only LIVE links are cached, with just what a redirect needs. Every Redis problem
is logged and treated like a cache miss ("fail open"): the app then simply uses
the database. The cache can make redirects faster, never make them fail.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

# A version in the key: if the cached shape ever changes, bump it and old keys are ignored
KEY_PREFIX = "redirect:v1:"
REDIS_ERRORS = (RedisError, OSError)


@dataclass(frozen=True)
class CachedRedirect:
    """What the redirect needs, without a database trip."""

    url_id: int
    original_url: str
    expires_at: datetime | None

    def to_json(self) -> str:
        return json.dumps(
            {
                "url_id": self.url_id,
                "original_url": self.original_url,
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> "CachedRedirect":
        data = json.loads(raw)
        expires_at = data["expires_at"]
        return cls(
            url_id=int(data["url_id"]),
            original_url=str(data["original_url"]),
            expires_at=datetime.fromisoformat(expires_at) if expires_at else None,
        )

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and self.expires_at <= now


class RedirectCache(Protocol):
    async def get(self, short_code: str) -> CachedRedirect | None: ...

    async def set(self, short_code: str, entry: CachedRedirect, now: datetime) -> None: ...

    async def invalidate(self, short_code: str) -> None: ...

    async def ping(self) -> bool | None:
        """True = reachable, False = down, None = no cache configured."""
        ...


class NullRedirectCache:
    """Used when REDIS_URL is empty: never stores anything (every lookup is a miss)."""

    async def get(self, short_code: str) -> CachedRedirect | None:
        return None

    async def set(self, short_code: str, entry: CachedRedirect, now: datetime) -> None:
        return None

    async def invalidate(self, short_code: str) -> None:
        return None

    async def ping(self) -> bool | None:
        return None


class RedisRedirectCache:
    def __init__(self, client: Redis, ttl_seconds: int) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def key(short_code: str) -> str:
        return f"{KEY_PREFIX}{short_code}"

    def ttl_for(self, entry: CachedRedirect, now: datetime) -> int:
        """15 minutes, or less if the link expires sooner (it must never outlive its expiry)."""
        if entry.expires_at is None:
            return self._ttl_seconds
        seconds_left = int((entry.expires_at - now).total_seconds())
        return min(self._ttl_seconds, seconds_left)

    async def get(self, short_code: str) -> CachedRedirect | None:
        try:
            raw = await self._client.get(self.key(short_code))
        except REDIS_ERRORS:
            logger.warning("Redis unavailable: reading %s from the database", short_code, exc_info=True)
            return None
        if raw is None:
            return None  # miss
        try:
            return CachedRedirect.from_json(raw)
        except (ValueError, KeyError, TypeError):
            logger.warning("Ignoring a broken cache entry for %s", short_code)
            return None

    async def set(self, short_code: str, entry: CachedRedirect, now: datetime) -> None:
        ttl = self.ttl_for(entry, now)
        if ttl < 1:
            return  # expires in under a second: not worth caching
        try:
            # SET key value EX ttl: Redis deletes the key by itself after ttl seconds
            await self._client.set(self.key(short_code), entry.to_json(), ex=ttl)
        except REDIS_ERRORS:
            logger.warning("Redis unavailable: %s not cached", short_code, exc_info=True)

    async def invalidate(self, short_code: str) -> None:
        try:
            await self._client.delete(self.key(short_code))
        except REDIS_ERRORS:
            # Worst case the old entry lives until its TTL ends (at most 15 minutes)
            logger.error("Could not invalidate cached redirect %s", short_code, exc_info=True)

    async def ping(self) -> bool | None:
        try:
            return bool(await self._client.ping())
        except REDIS_ERRORS:
            return False
