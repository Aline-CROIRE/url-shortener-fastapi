"""Failed logins are also limited per ACCOUNT: LOGIN_ACCOUNT_RATE_LIMIT (10/hour).

The per-IP limit cannot see a botnet: thousands of addresses each trying a few
passwords on ONE account. This second limit counts the FAILED logins of an email,
whatever the address. After 10 in an hour that email gets 429, even with the right
password (otherwise guess number 11 could still win).

- unknown emails are counted exactly like real ones: the 429 reveals nothing
- a successful login is not counted, and clears the count (the real user's typos)
- the email is never stored in the counter, only a hash of it
- trade-off: anyone can use up the budget of a known email, so the owner may have
  to wait up to an hour; it slows logins down, it never locks the account for good
"""

import logging
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.core.rate_limit import limiter

AUTH = "/api/v1/auth"
EMAIL = "aline@example.com"
PASSWORD = "a-long-passphrase"
ACCOUNT_LIMIT = 10


@pytest.fixture(autouse=True)
def many_ip_addresses(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Act like a botnet: lift the PER-IP limit, so only the per-account one is left."""
    monkeypatch.setattr(get_settings(), "login_rate_limit", "10000/minute")
    yield


def register(client: TestClient, email: str = EMAIL) -> None:
    response = client.post(f"{AUTH}/register/", json={"email": email, "password": PASSWORD})
    assert response.status_code == 202, response.text


def login(client: TestClient, email: str = EMAIL, password: str = "wrong-password") -> Any:
    return client.post(f"{AUTH}/login/", data={"username": email, "password": password})


def fail(client: TestClient, times: int, email: str = EMAIL) -> list[int]:
    return [login(client, email).status_code for _ in range(times)]


def test_the_11th_failed_login_for_one_account_is_429(db_client: TestClient) -> None:
    register(db_client)

    assert fail(db_client, ACCOUNT_LIMIT) == [401] * ACCOUNT_LIMIT
    response = login(db_client)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "too_many_requests"
    assert int(response.headers["retry-after"]) >= 1


def test_the_right_password_does_not_get_past_the_account_limit(db_client: TestClient) -> None:
    register(db_client)
    fail(db_client, ACCOUNT_LIMIT)

    assert login(db_client, password=PASSWORD).status_code == 429


def test_unknown_and_real_emails_look_exactly_the_same(db_client: TestClient) -> None:
    register(db_client)
    fail(db_client, ACCOUNT_LIMIT, EMAIL)
    fail(db_client, ACCOUNT_LIMIT, "nobody@example.com")

    real = login(db_client, EMAIL)
    unknown = login(db_client, "nobody@example.com")

    assert real.status_code == unknown.status_code == 429
    assert real.json() == unknown.json()


def test_successful_logins_do_not_count(db_client: TestClient) -> None:
    register(db_client)

    codes = [login(db_client, password=PASSWORD).status_code for _ in range(ACCOUNT_LIMIT + 5)]

    assert codes == [200] * (ACCOUNT_LIMIT + 5)


def test_a_successful_login_clears_the_failures(db_client: TestClient) -> None:
    register(db_client)
    fail(db_client, ACCOUNT_LIMIT - 1)  # typos

    assert login(db_client, password=PASSWORD).status_code == 200

    assert fail(db_client, ACCOUNT_LIMIT - 1) == [401] * (ACCOUNT_LIMIT - 1)  # a fresh count


def test_other_accounts_are_not_affected(db_client: TestClient) -> None:
    register(db_client)
    register(db_client, "other@example.com")
    fail(db_client, ACCOUNT_LIMIT + 1)

    assert login(db_client, "other@example.com", PASSWORD).status_code == 200


def test_letter_case_and_spaces_do_not_give_a_new_count(db_client: TestClient) -> None:
    register(db_client)
    spellings = [EMAIL, EMAIL.upper(), f"  {EMAIL} ", "Aline@Example.com"]
    for n in range(ACCOUNT_LIMIT):
        login(db_client, spellings[n % len(spellings)])

    assert login(db_client, "ALINE@EXAMPLE.COM ").status_code == 429


def test_the_email_itself_is_never_stored_in_the_counter(db_client: TestClient) -> None:
    fail(db_client, 3, "secret.person@example.com")

    stored_keys = " ".join(str(key) for key in limiter.limiter.storage.storage)

    assert "secret.person" not in stored_keys
    assert "example.com" not in stored_keys


def test_hitting_the_account_limit_is_logged_with_a_masked_email(
    db_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    fail(db_client, ACCOUNT_LIMIT + 1, "aline.smith@example.com")

    messages = [r.getMessage() for r in caplog.records if r.name == "app.security"]
    hits = [m for m in messages if "account login limit" in m.lower()]
    assert hits
    assert all("a***@example.com" in m and "aline.smith" not in m for m in hits)


def test_the_account_limit_comes_from_the_settings() -> None:
    assert Settings(_env_file=None).login_account_rate_limit == "10/hour"  # type: ignore[call-arg]

    with pytest.raises(ValidationError, match="LOGIN_ACCOUNT_RATE_LIMIT"):
        Settings(_env_file=None, login_account_rate_limit="0/hour")  # type: ignore[call-arg]
