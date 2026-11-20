"""Who is calling, network-wise: the client's IP address."""

from starlette.requests import Request


def client_ip(request: Request) -> str:
    """Return the client's IP address (the rate-limit key, and what security logs show).

    request.client.host is the direct peer. Behind a reverse proxy, run Uvicorn with
    --proxy-headers and trusted --forwarded-allow-ips so it becomes the real client
    IP. X-Forwarded-For is never read here: anyone can fake it, and a fake one per
    request would give every attempt a "new" IP and switch the rate limit off.
    """
    return request.client.host if request.client else "unknown"
