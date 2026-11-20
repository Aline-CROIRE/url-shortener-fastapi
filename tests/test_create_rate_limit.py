"""Creating short URLs is limited per USER (OWASP API4 + API6:2023).

A URL shortener's sensitive business flow is mass link creation: a spammer could
fill it with thousands of phishing links in a minute. The Free limit (10 active
URLs) does not stop that: deleting and creating again is a loop, and Premium has
no URL limit. So creating is limited to CREATE_URL_RATE_LIMIT (30/minute) per user.

The key is the USER (from the token), not the IP address: changing networks does
not give a spammer a fresh budget, and many users behind one office IP do not
share one.
"""

import logging
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.requests import Request

from app.core.config import Settings
from app.core.rate_limit import user_rate_limit_key
from app.models import UserTier
from tests.conftest import AuthedUser

URLS = "/api/v1/urls"
TARGET = "https://www.python.org/"
LIMIT = 30

MakeUser = Callable[..., AuthedUser]


def create(client: TestClient, user: AuthedUser | None = None) -> Any:
    headers = user.headers if user else {}
    return client.post(f"{URLS}/", json={"original_url": TARGET}, headers=headers)


def use_up_the_budget(client: TestClient, user: AuthedUser) -> list[str]:
    codes = []
    for _ in range(LIMIT):
        response = create(client, user)
        assert response.status_code == 201, response.text
        codes.append(response.json()["short_code"])
    return codes


def test_the_31st_create_in_a_minute_is_429(db_client: TestClient, make_user: MakeUser) -> None:
    user = make_user(UserTier.PREMIUM)  # Premium: no 10-URL limit in the way
    use_up_the_budget(db_client, user)

    response = create(db_client, user)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "too_many_requests"
    assert 1 <= int(response.headers["retry-after"]) <= 60


def test_deleting_does_not_give_the_budget_back(db_client: TestClient, make_user: MakeUser) -> None:
    user = make_user(UserTier.PREMIUM)
    for code in use_up_the_budget(db_client, user):
        assert db_client.delete(f"{URLS}/{code}/", headers=user.headers).status_code == 204

    # A create/delete loop is exactly how the Free limit could be farmed
    assert create(db_client, user).status_code == 429


def test_every_user_has_their_own_budget(db_client: TestClient, make_user: MakeUser) -> None:
    spammer, someone_else = make_user(UserTier.PREMIUM), make_user(UserTier.PREMIUM)
    use_up_the_budget(db_client, spammer)

    assert create(db_client, spammer).status_code == 429
    # Same IP address (the test client), different user: not blocked
    assert create(db_client, someone_else).status_code == 201


def test_other_actions_still_work_at_the_limit(db_client: TestClient, make_user: MakeUser) -> None:
    user = make_user(UserTier.PREMIUM)
    code = use_up_the_budget(db_client, user)[0]

    assert db_client.get(f"{URLS}/", headers=user.headers).status_code == 200
    put = db_client.put(f"{URLS}/{code}/", json={"title": "still mine"}, headers=user.headers)
    assert put.status_code == 200
    assert db_client.get(f"/{code}/").status_code == 302  # links keep working


def test_without_a_token_it_is_still_401_not_429(
    db_client: TestClient, make_user: MakeUser
) -> None:
    use_up_the_budget(db_client, make_user(UserTier.PREMIUM))

    response = create(db_client)  # anonymous: refused before any counting

    assert response.status_code == 401


def test_hitting_the_create_limit_is_logged(
    db_client: TestClient, make_user: MakeUser, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    user = make_user(UserTier.PREMIUM)
    use_up_the_budget(db_client, user)

    create(db_client, user)

    messages = [r.getMessage() for r in caplog.records if r.name == "app.security"]
    assert any("Rate limit hit" in m and f"user:{user.id}" in m for m in messages)


def request_with(user_id: int | None, host: str) -> Request:
    request = Request({"type": "http", "headers": [], "client": (host, 1234)})
    if user_id is not None:
        request.state.user_id = user_id
    return request


def test_the_key_is_the_user_whatever_the_network() -> None:
    at_home = user_rate_limit_key(request_with(7, "1.2.3.4"))
    on_the_phone = user_rate_limit_key(request_with(7, "2001:db8::1"))

    assert at_home == on_the_phone == "user:7"


def test_without_a_user_the_key_falls_back_to_the_client_address() -> None:
    assert user_rate_limit_key(request_with(None, "1.2.3.4")) == "1.2.3.4"


def test_the_create_limit_comes_from_the_settings() -> None:
    assert Settings(_env_file=None).create_url_rate_limit == "30/minute"  # type: ignore[call-arg]

    with pytest.raises(ValidationError, match="CREATE_URL_RATE_LIMIT"):
        Settings(_env_file=None, create_url_rate_limit="lots")  # type: ignore[call-arg]
