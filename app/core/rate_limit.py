"""Login rate limiting with slowapi: LOGIN_RATE_LIMIT ("5/minute") per client IP address.

Storage is IN MEMORY: every worker process keeps its own counters, so with N
Gunicorn workers one IP can try up to N x 5 times a minute, and a restart resets
them. Module 8 moves the counters to Redis (storage_uri="redis://..."), shared by
all workers and kept across restarts.
"""

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


def login_rate_limit() -> str:
    """Return the configured login limit (slowapi calls this when it checks)."""
    return get_settings().login_rate_limit


# Module 8: storage_uri="redis://..." so all workers share one count
limiter = Limiter(key_func=client_ip, storage_uri="memory://")


async def rate_limit_exceeded_handler(request: Request, exc: Exception) -> JSONResponse:
    """Answer 429 in our JSON error shape, with Retry-After, and log the hit."""
    retry_after = 60  # fallback: one minute
    if isinstance(exc, RateLimitExceeded) and exc.limit is not None:
        retry_after = exc.limit.limit.get_expiry()  # the window length, in seconds
    security_logger.warning(
        "Login rate limit hit: ip=%s path=%s",
        clean_for_log(client_ip(request)),
        clean_for_log(request.url.path),
    )
    return error_response(
        429,
        "too_many_requests",
        "Too many login attempts from your address. Please try again in a minute.",
        headers={"Retry-After": str(retry_after)},
    )
