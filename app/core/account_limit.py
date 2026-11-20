"""Failed logins counted per ACCOUNT (email): LOGIN_ACCOUNT_RATE_LIMIT, e.g. "10/hour".

The per-IP login limit cannot see a botnet (many addresses, one account). This one
counts the FAILED logins of one email, from anywhere. Unknown emails are counted
the same way, so being blocked reveals nothing about which accounts exist.

It uses the same storage as the other limits (Redis when configured, else memory),
under a keyed hash of the email (HMAC with the JWT secret): the counters never hold
an email, and without the secret a counter cannot be matched to one by guessing.
"""

import hashlib
import hmac
import logging
import time
from collections.abc import Callable
from typing import TypeVar

from limits import RateLimitItem, parse
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter, RateLimiter

from app.core.config import get_settings
from app.core.rate_limit import limiter

logger = logging.getLogger(__name__)

NAMESPACE = "login-account"
# After the storage (Redis) fails, use memory for this long before trying it again,
# so a dead Redis does not add a timeout to every single login
RETRY_STORAGE_AFTER_SECONDS = 30.0

# The fallback when the shared storage does not answer: this worker's own memory
_memory = MovingWindowRateLimiter(MemoryStorage())
# For each limiter (by id): until when its storage is treated as down
_storage_down_until: dict[int, float] = {}

T = TypeVar("T")


def _with_fallback(operation: Callable[[RateLimiter], T]) -> T:
    """Run a counter operation on the shared storage, or in memory if it is down.

    Never raises because Redis is unreachable: a login must not fail with a 500
    because the counters are unavailable. The per-account limit keeps counting in
    this worker's memory meanwhile (the per-IP limit has its own fallback).
    """
    key = id(limiter)
    if time.monotonic() < _storage_down_until.get(key, 0.0):
        return operation(_memory)
    try:
        return operation(limiter.limiter)
    except Exception:
        logger.warning("Rate-limit storage unreachable: counting failed logins in memory")
        _storage_down_until[key] = time.monotonic() + RETRY_STORAGE_AFTER_SECONDS
        return operation(_memory)


def _account_key(email: str) -> str:
    """Return a keyed hash of the normalised email (no email in the counters)."""
    secret = get_settings().jwt_secret_key.get_secret_value().encode()
    normalized = email.strip().lower().encode()
    return hmac.new(secret, normalized, hashlib.sha256).hexdigest()


def _limit() -> RateLimitItem:
    """Return the configured per-account limit (read each time: settings may change)."""
    return parse(get_settings().login_account_rate_limit)


def is_blocked(email: str) -> bool:
    """Return True if this email already used up its failed-login budget."""
    key = _account_key(email)
    return not _with_fallback(lambda counter: counter.test(_limit(), NAMESPACE, key))


def record_failure(email: str) -> None:
    """Count one failed login for this email."""
    key = _account_key(email)
    _with_fallback(lambda counter: counter.hit(_limit(), NAMESPACE, key))


def clear_failures(email: str) -> None:
    """Forget the failures after a successful login (the real user's typos)."""
    key = _account_key(email)
    _with_fallback(lambda counter: counter.clear(_limit(), NAMESPACE, key))


def retry_after_seconds() -> int:
    """Return the length of the limit's window, in seconds (for Retry-After)."""
    return _limit().get_expiry()
