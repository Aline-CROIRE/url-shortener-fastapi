"""Read visitor details (IP address, user agent, referrer) from a request."""

from ipaddress import IPv4Address, IPv6Address, ip_address
from urllib.parse import urlsplit

from fastapi import Request

from app.services.redirect import VisitorInfo

MAX_USER_AGENT_LENGTH = 512
MAX_REFERRER_LENGTH = 2048


def parse_ip(value: str | None) -> IPv4Address | IPv6Address | None:
    """Return a real IP address, or None.

    For example, TestClient reports the host "testclient", which becomes None.
    """
    if not value:
        return None
    try:
        return ip_address(value)
    except ValueError:
        return None


def clip(value: str | None, max_length: int) -> str | None:
    """Turn an empty value into None and cut a very long value.

    Long header values are cut so nobody can flood the database.
    """
    if not value:
        return None
    return value[:max_length]


def clean_referrer(value: str | None) -> str | None:
    """Keep the Referer header only when it is a real http(s) URL.

    This works like Django's URLField. Anything else ("", "android-app://...",
    random text) becomes None, which analytics shows as "Direct".
    """
    referrer = clip(value, MAX_REFERRER_LENGTH)
    if referrer is None:
        return None
    try:
        parts = urlsplit(referrer)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return referrer


def visitor_from_request(request: Request) -> VisitorInfo:
    """Collect the visitor's IP address, user agent and referrer."""
    # request.client.host is the direct peer. Behind a reverse proxy, run Uvicorn
    # with --proxy-headers (and trusted --forwarded-allow-ips) so this becomes the
    # real visitor IP. Never read X-Forwarded-For yourself: anyone can fake it.
    host = request.client.host if request.client else None
    return VisitorInfo(
        ip_address=parse_ip(host),
        user_agent=clip(request.headers.get("user-agent"), MAX_USER_AGENT_LENGTH),
        referrer=clean_referrer(request.headers.get("referer")),  # sic: "referer"
    )
