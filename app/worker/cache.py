"""The Celery worker's (synchronous) Redis client, used to clear cached redirects."""

import logging
from collections.abc import Sequence
from functools import lru_cache

from redis import Redis
from redis.exceptions import RedisError

from app.cache import RedisRedirectCache
from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Keys deleted per DELETE command: a huge single command would block Redis
DELETE_BATCH_SIZE = 500


@lru_cache
def get_redis() -> Redis | None:
    """Return the worker's Redis client for the cache DB, or None without REDIS_URL.

    Created on first use in each worker process (like the database engine).
    """
    redis_url = get_settings().redis_url
    if not redis_url:
        return None
    return Redis.from_url(redis_url, socket_connect_timeout=1, socket_timeout=2)


def clear_cached_redirects(client: Redis | None, short_codes: Sequence[str]) -> None:
    """Delete the cached redirects of these short codes; a Redis error is only logged.

    The database change is already committed. If Redis is down, every entry
    still disappears by itself when its TTL ends (never after the link's expiry).
    """
    if client is None or not short_codes:
        return
    try:
        for start in range(0, len(short_codes), DELETE_BATCH_SIZE):
            batch = short_codes[start : start + DELETE_BATCH_SIZE]
            client.delete(*(RedisRedirectCache.key(code) for code in batch))
    except (RedisError, OSError):
        logger.warning(
            "Could not clear %d cached redirects; they expire by their TTL",
            len(short_codes),
            exc_info=True,
        )
