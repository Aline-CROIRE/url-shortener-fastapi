"""Rate limits with slowapi: login and register per client, URL creation per user.

Sliding ("moving") window: a fixed window would allow 5 tries at 0:59 and 5 more
at 1:00, so 10 in one second; a sliding one counts the LAST minute at every moment.

Storage: REDIS when configured (RATE_LIMIT_STORAGE_URI, or REDIS_URL): one count
shared by every Gunicorn worker and kept across restarts. Without Redis, each worker
counts in its own memory (with N workers, "5 per minute" is really N x 5).
If Redis stops answering, the limits keep counting in memory (per worker) and go
back to Redis once it answers again: never a 500, never a hang (short timeouts).
"""

import ipaddress
import logging
from typing import Any, cast

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded

from app.core.client import client_ip
from app.core.config import get_settings
from app.core.error_handlers import error_response
from app.core.logging import clean_for_log

security_logger = logging.getLogger("app.security")

# One home or phone usually gets a whole IPv6 /64 network: counting each of its
# 2^64 addresses apart would give one client endless fresh counters
IPV6_PREFIX_LENGTH = 64


def rate_limit_key(request: Request) -> str:
    """Return the bucket a request is counted in: one per client.

    IPv4: the address itself. An IPv4 address written as IPv6 (::ffff:1.2.3.4)
    is the same client, so it counts as 1.2.3.4. IPv6: the /64 network it is in.
    Anything that is not an IP address (a test client) is used as it is.
    """
    host = client_ip(request)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/{IPV6_PREFIX_LENGTH}", strict=False))
    return str(address)


def user_rate_limit_key(request: Request) -> str:
    """Return the bucket for a limit on LOGGED-IN actions: the user, whatever the network.

    get_current_user stores the user id on the request before the endpoint runs,
    so a spammer cannot get a fresh budget by switching networks, and colleagues
    behind one office IP do not share a budget. Without a user (never the case on
    the protected endpoints that use it) it falls back to the client address.
    """
    user_id = getattr(request.state, "user_id", None)
    return f"user:{user_id}" if user_id is not None else rate_limit_key(request)


def create_url_rate_limit() -> str:
    """Return the configured limit on creating short URLs, per user."""
    return get_settings().create_url_rate_limit


def login_rate_limit() -> str:
    """Return the configured login limit (slowapi calls this when it checks)."""
    return get_settings().login_rate_limit


def register_rate_limit() -> str:
    """Return the configured register limit (slowapi calls this when it checks)."""
    return get_settings().register_rate_limit


# A dead Redis must fail fast, not hang every login (same values as the redirect cache)
REDIS_TIMEOUT_SECONDS = 0.5
# Our keys in a shared Redis start with this (the redirect cache uses "redirect:")
KEY_PREFIX = "url-shortener-limits"


def build_limiter(storage_uri: str, key_prefix: str = KEY_PREFIX) -> Limiter:
    """Create the rate limiter on the given storage ("memory://" or "redis://...").

    in_memory_fallback_enabled: if the storage stops answering, slowapi counts in
    memory instead and checks the storage again later (with growing pauses).
    """
    options: dict[str, Any] = {}
    if storage_uri.startswith(("redis://", "rediss://")):
        options = {
            "socket_connect_timeout": REDIS_TIMEOUT_SECONDS,
            "socket_timeout": REDIS_TIMEOUT_SECONDS,
        }
    return Limiter(
        key_func=rate_limit_key,
        storage_uri=storage_uri,
        # slowapi types the options as text, but the Redis client needs numbers here
        storage_options=cast(dict[str, str], options),
        strategy="moving-window",
        in_memory_fallback_enabled=True,
        key_prefix=key_prefix,
    )


limiter = build_limiter(get_settings().rate_limit_storage)


async def rate_limit_exceeded_handler(request: Request, exc: Exception) -> JSONResponse:
    """Answer 429 in our JSON error shape, with Retry-After, and log the hit."""
    retry_after = 60  # fallback: one minute
    if isinstance(exc, RateLimitExceeded) and exc.limit is not None:
        retry_after = exc.limit.limit.get_expiry()  # the window length, in seconds
    security_logger.warning(
        "Rate limit hit: client=%s path=%s",
        clean_for_log(user_rate_limit_key(request)),  # the user when known, else the address
        clean_for_log(request.url.path),
    )
    return error_response(
        429,
        "too_many_requests",
        "Too many attempts. Please try again later (see Retry-After).",
        headers={"Retry-After": str(retry_after)},
    )
