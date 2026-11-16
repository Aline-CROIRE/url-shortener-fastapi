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
        """Turn this entry into a JSON string for Redis."""
        return json.dumps(
            {
                "url_id": self.url_id,
                "original_url": self.original_url,
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> "CachedRedirect":
        """Build an entry from a JSON string read from Redis."""
        data = json.loads(raw)
        expires_at = data["expires_at"]
        return cls(
            url_id=int(data["url_id"]),
            original_url=str(data["original_url"]),
            expires_at=datetime.fromisoformat(expires_at) if expires_at else None,
        )

    def is_expired(self, now: datetime) -> bool:
        """Return True if the link has an expiry and it is reached at `now`."""
        return self.expires_at is not None and self.expires_at <= now


class RedirectCache(Protocol):
    """What every redirect cache must offer (the Redis one and the "null" one)."""

    async def get(self, short_code: str) -> CachedRedirect | None:
        """Return the cached entry, or None on a miss."""
        ...

    async def set(self, short_code: str, entry: CachedRedirect, now: datetime) -> None:
        """Store the entry for this short code."""
        ...

    async def invalidate(self, short_code: str) -> None:
        """Remove the cached entry for this short code."""
        ...

    async def ping(self) -> bool | None:
        """Check the cache.

        Returns:
            True if reachable, False if down, None if no cache is configured.
        """
        ...


class NullRedirectCache:
    """Used when REDIS_URL is empty: never stores anything (every lookup is a miss)."""

    async def get(self, short_code: str) -> CachedRedirect | None:
        """Return None: there is never a cached entry."""
        return None

    async def set(self, short_code: str, entry: CachedRedirect, now: datetime) -> None:
        """Do nothing: nothing is stored."""
        return None

    async def invalidate(self, short_code: str) -> None:
        """Do nothing: there is nothing to remove."""
        return None

    async def ping(self) -> bool | None:
        """Return None: no cache is configured."""
        return None


class RedisRedirectCache:
    """Redirect cache stored in Redis; every Redis error counts as a miss."""

    def __init__(self, client: Redis, ttl_seconds: int) -> None:
        """Keep the Redis client and the normal lifetime of an entry in seconds."""
        self._client = client
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def key(short_code: str) -> str:
        """Return the Redis key for a short code."""
        return f"{KEY_PREFIX}{short_code}"

    def ttl_for(self, entry: CachedRedirect, now: datetime) -> int:
        """Return how many seconds to keep the entry.

        15 minutes, or less if the link expires sooner (it must never outlive its expiry).
        """
        if entry.expires_at is None:
            return self._ttl_seconds
        seconds_left = int((entry.expires_at - now).total_seconds())
        return min(self._ttl_seconds, seconds_left)

    async def get(self, short_code: str) -> CachedRedirect | None:
        """Return the cached entry, or None on a miss, a broken entry or a Redis error."""
        try:
            raw = await self._client.get(self.key(short_code))
        except REDIS_ERRORS:
            logger.warning(
                "Redis unavailable: reading %s from the database", short_code, exc_info=True
            )
            return None
        if raw is None:
            return None  # miss
        try:
            return CachedRedirect.from_json(raw if isinstance(raw, str) else raw.decode())
        except (ValueError, KeyError, TypeError):
            logger.warning("Ignoring a broken cache entry for %s", short_code)
            return None

    async def set(self, short_code: str, entry: CachedRedirect, now: datetime) -> None:
        """Store the entry until its TTL ends; skip it if Redis is down."""
        ttl = self.ttl_for(entry, now)
        if ttl < 1:
            return  # expires in under a second: not worth caching
        try:
            # SET key value EX ttl: Redis deletes the key by itself after ttl seconds
            await self._client.set(self.key(short_code), entry.to_json(), ex=ttl)
        except REDIS_ERRORS:
            logger.warning("Redis unavailable: %s not cached", short_code, exc_info=True)

    async def invalidate(self, short_code: str) -> None:
        """Delete the cached entry so the next redirect reads the database."""
        try:
            await self._client.delete(self.key(short_code))
        except REDIS_ERRORS:
            # Worst case the old entry lives until its TTL ends (at most 15 minutes)
            logger.error("Could not invalidate cached redirect %s", short_code, exc_info=True)

    async def ping(self) -> bool | None:
        """Return True if Redis answers, False if it does not."""
        try:
            return bool(await self._client.ping())
        except REDIS_ERRORS:
            return False
