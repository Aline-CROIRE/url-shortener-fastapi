from ipaddress import IPv4Address, IPv6Address

import pytest
from starlette.requests import Request

from app.api.visitor import (
    MAX_REFERRER_LENGTH,
    MAX_USER_AGENT_LENGTH,
    parse_ip,
    visitor_from_request,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("41.186.0.10", IPv4Address("41.186.0.10")),
        ("2001:db8::1", IPv6Address("2001:db8::1")),
        ("testclient", None),  # FastAPI's TestClient
        ("", None),
        (None, None),
    ],
)
def test_parse_ip(value: str | None, expected: object) -> None:
    assert parse_ip(value) == expected


def make_request(client: tuple[str, int] | None, headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "client": client,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


def test_visitor_from_request_reads_ip_and_headers() -> None:
    request = make_request(
        ("41.186.0.10", 51000), {"User-Agent": "Mozilla/5.0", "Referer": "https://x.com/"}
    )

    visitor = visitor_from_request(request)

    assert visitor.ip_address == IPv4Address("41.186.0.10")
    assert visitor.user_agent == "Mozilla/5.0"
    assert visitor.referrer == "https://x.com/"


def test_visitor_from_request_handles_missing_values() -> None:
    visitor = visitor_from_request(make_request(None, {}))

    assert (visitor.ip_address, visitor.user_agent, visitor.referrer) == (None, None, None)


def test_huge_headers_are_cut() -> None:
    long_referrer = "https://example.com/" + "b" * 10_000  # long, but a real URL
    request = make_request(None, {"User-Agent": "a" * 10_000, "Referer": long_referrer})

    visitor = visitor_from_request(request)

    assert len(visitor.user_agent) == MAX_USER_AGENT_LENGTH
    assert len(visitor.referrer) == MAX_REFERRER_LENGTH
