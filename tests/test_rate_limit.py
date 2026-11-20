"""Login rate limit: at most 5 login attempts per minute per IP address (brief test #6).

It slows down password guessing. It counts EVERY attempt from one IP (right or
wrong password, any email), so neither "try many passwords on one account" nor
"try one password on many accounts" (password spraying) gets past it.
"""

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

from app.core.config import Settings
from app.core.rate_limit import rate_limit_exceeded_handler
from app.main import create_app
from tests.conftest import AuthedUser

LOGIN = "/api/v1/auth/login/"
EMAIL = "aline@example.com"
PASSWORD = "a-long-passphrase"
LIMIT = 5

MakeUser = Callable[..., AuthedUser]


def attempt(
    client: TestClient,
    email: str = "nobody@example.com",
    password: str = "wrong-password",
    path: str = LOGIN,
    headers: dict[str, str] | None = None,
) -> int:
    response = client.post(path, data={"username": email, "password": password}, headers=headers)
    return response.status_code


def register(client: TestClient) -> None:
    response = client.post("/api/v1/auth/register/", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 202


def test_the_6th_login_in_a_minute_is_429(db_client: TestClient) -> None:
    assert [attempt(db_client) for _ in range(LIMIT)] == [401] * LIMIT

    response = db_client.post(LOGIN, data={"username": "x@example.com", "password": "x"})

    assert response.status_code == 429
    error = response.json()["error"]
    assert error["code"] == "too_many_requests"
    assert "try again" in error["message"].lower()
    assert 1 <= int(response.headers["retry-after"]) <= 60  # when to come back


def test_the_right_password_does_not_get_past_the_limit(db_client: TestClient) -> None:
    register(db_client)
    for _ in range(LIMIT):
        attempt(db_client, EMAIL)

    assert attempt(db_client, EMAIL, PASSWORD) == 429  # no token, even with the right password


def test_successful_logins_count_too(db_client: TestClient) -> None:
    register(db_client)

    codes = [attempt(db_client, EMAIL, PASSWORD) for _ in range(LIMIT + 1)]

    assert codes == [200] * LIMIT + [429]


def test_changing_the_email_does_not_reset_the_limit(db_client: TestClient) -> None:
    # One password tried on many accounts ("password spraying") is limited too
    codes = [attempt(db_client, f"user{n}@example.com") for n in range(LIMIT + 1)]

    assert codes == [401] * LIMIT + [429]


def test_both_paths_share_one_counter(db_client: TestClient) -> None:
    codes = [attempt(db_client, path=LOGIN) for _ in range(3)]
    codes += [attempt(db_client, path=LOGIN.rstrip("/")) for _ in range(3)]

    assert codes == [401] * LIMIT + [429]


def test_a_fake_x_forwarded_for_header_does_not_bypass_the_limit(db_client: TestClient) -> None:
    # Anyone can send this header: trusting it would give every request a "new" IP
    codes = [
        attempt(db_client, headers={"X-Forwarded-For": f"10.0.0.{n}"}) for n in range(LIMIT + 1)
    ]

    assert codes[-1] == 429


def test_other_endpoints_are_not_limited(db_client: TestClient) -> None:
    for _ in range(LIMIT + 1):
        attempt(db_client)

    assert db_client.get("/health").status_code == 200
    register(db_client)  # 201, not 429


@pytest.fixture
def two_clients(test_settings: Settings) -> Iterator[tuple[TestClient, TestClient]]:
    """Two clients with different IP addresses, each on its own copy of the real app.

    One app per client: each TestClient runs the app's startup in its own event
    loop, so they must not share one app's database engine. The rate limiter is
    shared (it belongs to the process), exactly as in production.
    """
    with (
        TestClient(create_app(test_settings), client=("10.0.0.1", 50000)) as first,
        TestClient(create_app(test_settings), client=("10.0.0.2", 50000)) as second,
    ):
        yield first, second


def test_every_ip_address_has_its_own_limit(two_clients: tuple[TestClient, TestClient]) -> None:
    first, second = two_clients
    for _ in range(LIMIT):
        attempt(first)

    assert attempt(first) == 429
    assert attempt(second) == 401  # someone else is not blocked by the first one


def test_the_limit_comes_from_the_settings() -> None:
    assert Settings(_env_file=None).login_rate_limit == "5/minute"  # type: ignore[call-arg]
    assert Settings(login_rate_limit="10/hour").login_rate_limit == "10/hour"


@pytest.mark.parametrize("value", ["", "lots", "5 per", "-1/minute"])
def test_a_broken_limit_stops_the_app_at_startup(value: str) -> None:
    with pytest.raises(ValidationError, match="LOGIN_RATE_LIMIT"):
        Settings(login_rate_limit=value)


@pytest.mark.anyio
async def test_the_429_handler_falls_back_to_one_minute() -> None:
    # Only RateLimitExceeded carries the window length; anything else gets 60 seconds
    request = Request({"type": "http", "method": "POST", "path": LOGIN, "headers": []})

    response = await rate_limit_exceeded_handler(request, RuntimeError("no limit details"))

    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
