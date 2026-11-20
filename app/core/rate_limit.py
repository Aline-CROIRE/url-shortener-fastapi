"""Rate limits with slowapi: login and register per client, URL creation per user.

Sliding ("moving") window: a fixed window would allow 5 tries at 0:59 and 5 more
at 1:00, so 10 in one second; a sliding one counts the LAST minute at every moment.

Storage is IN MEMORY: every worker process keeps its own counters, so with N
Gunicorn workers one client can try up to N x 5 times a minute, and a restart
resets them. Module 8 moves the counters to Redis (storage_uri="redis://..."),
shared by all workers and kept across restarts.
"""

import ipaddress
import logging

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


# Module 8: storage_uri="redis://..." so all workers share one count
limiter = Limiter(key_func=rate_limit_key, storage_uri="memory://", strategy="moving-window")


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
