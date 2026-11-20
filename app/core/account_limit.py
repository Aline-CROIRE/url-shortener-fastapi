"""Failed logins counted per ACCOUNT (email): LOGIN_ACCOUNT_RATE_LIMIT, e.g. "10/hour".

The per-IP login limit cannot see a botnet (many addresses, one account). This one
counts the FAILED logins of one email, from anywhere. Unknown emails are counted
the same way, so being blocked reveals nothing about which accounts exist.

It uses the same storage as the other limits (memory now, Redis when configured),
under a keyed hash of the email (HMAC with the JWT secret): the counters never hold
an email, and without the secret a counter cannot be matched to one by guessing.
"""

import hashlib
import hmac

from limits import RateLimitItem, parse

from app.core.config import get_settings
from app.core.rate_limit import limiter

NAMESPACE = "login-account"


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
    return not limiter.limiter.test(_limit(), NAMESPACE, _account_key(email))


def record_failure(email: str) -> None:
    """Count one failed login for this email."""
    limiter.limiter.hit(_limit(), NAMESPACE, _account_key(email))


def clear_failures(email: str) -> None:
    """Forget the failures after a successful login (the real user's typos)."""
    limiter.limiter.clear(_limit(), NAMESPACE, _account_key(email))


def retry_after_seconds() -> int:
    """Return the length of the limit's window, in seconds (for Retry-After)."""
    return _limit().get_expiry()
