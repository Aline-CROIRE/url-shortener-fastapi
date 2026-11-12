from ipaddress import IPv4Address, IPv6Address, ip_address

from fastapi import Request

from app.services.redirect import VisitorInfo

MAX_USER_AGENT_LENGTH = 512
MAX_REFERRER_LENGTH = 2048


def parse_ip(value: str | None) -> IPv4Address | IPv6Address | None:
    """A real IP address, or None (e.g. TestClient reports the host "testclient")."""
    if not value:
        return None
    try:
        return ip_address(value)
    except ValueError:
        return None


def clip(value: str | None, max_length: int) -> str | None:
    """Empty -> None; very long header values are cut so nobody can flood the DB."""
    if not value:
        return None
    return value[:max_length]


def visitor_from_request(request: Request) -> VisitorInfo:
    # request.client.host is the direct peer. Behind a reverse proxy, run Uvicorn
    # with --proxy-headers (and trusted --forwarded-allow-ips) so this becomes the
    # real visitor IP. Never read X-Forwarded-For yourself: anyone can fake it.
    host = request.client.host if request.client else None
    return VisitorInfo(
        ip_address=parse_ip(host),
        user_agent=clip(request.headers.get("user-agent"), MAX_USER_AGENT_LENGTH),
        referrer=clip(request.headers.get("referer"), MAX_REFERRER_LENGTH),  # sic: "referer"
    )
