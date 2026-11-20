"""Security logging and clean errors.

Logged (so an admin can see an attack): failed logins, 403s and rate-limit hits.
Never logged: passwords, tokens, the Authorization header, or a full email address.
Logs must not be forgeable: a line break sent by a client never starts a fake line.
Clients never see a stack trace; the server log keeps it.
"""

import logging
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models import UserTier
from tests.conftest import AuthedUser

LOGIN = "/api/v1/auth/login/"
URLS = "/api/v1/urls"
TARGET = "https://www.python.org/"
SECURITY_LOGGER = "app.security"

MakeUser = Callable[..., AuthedUser]


@pytest.fixture
def security_logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO)
    return caplog


def security_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == SECURITY_LOGGER]


def login(client: TestClient, email: str, password: str = "wrong-password-123") -> Any:
    return client.post(LOGIN, data={"username": email, "password": password})


# ---------- failed logins ----------


def test_a_failed_login_is_logged_without_the_password_or_full_email(
    db_client: TestClient, security_logs: pytest.LogCaptureFixture
) -> None:
    login(db_client, "aline.smith@example.com")

    (message,) = security_messages(security_logs)
    assert "failed login" in message.lower()
    assert "a***@example.com" in message  # enough to spot a pattern, not the whole address
    assert "aline.smith" not in message
    assert "testclient" in message  # the IP address of the client
    assert "wrong-password-123" not in security_logs.text


def test_a_successful_login_is_not_logged_as_a_failure(
    db_client: TestClient, security_logs: pytest.LogCaptureFixture
) -> None:
    db_client.post(
        "/api/v1/auth/register/", json={"email": "ok@example.com", "password": "a-long-passphrase"}
    )

    assert login(db_client, "ok@example.com", "a-long-passphrase").status_code == 200
    assert security_messages(security_logs) == []


@pytest.mark.parametrize(
    "email",
    [
        "x@example.com\n2026-01-01 INFO app: admin logged in",  # a fake log line
        "x@example.com\r\nFORGED",
        "evil\x1b[31m@example.com",  # terminal colour codes
        "a" * 5000 + "@example.com",
        "no-at-sign",
    ],
    ids=["newline", "crlf", "escape_codes", "huge", "no_at_sign"],
)
def test_a_client_cannot_forge_or_flood_a_log_line(
    db_client: TestClient, security_logs: pytest.LogCaptureFixture, email: str
) -> None:
    login(db_client, email)

    (message,) = security_messages(security_logs)
    assert "\n" not in message and "\r" not in message and "\x1b" not in message
    assert len(message) < 300


# ---------- 403s and 429s ----------


def test_a_403_is_logged_with_who_and_what(
    db_client: TestClient, make_user: MakeUser, security_logs: pytest.LogCaptureFixture
) -> None:
    alice, bob = make_user(), make_user()
    code = db_client.post(f"{URLS}/", json={"original_url": TARGET}, headers=alice.headers).json()[
        "short_code"
    ]

    db_client.delete(f"{URLS}/{code}/", headers=bob.headers)

    (message,) = security_messages(security_logs)
    assert "403" in message
    assert f"user_id={bob.id}" in message
    assert f"DELETE /api/v1/urls/{code}/" in message
    assert "forbidden" in message


def test_tier_and_limit_403s_are_logged_too(
    db_client: TestClient, make_user: MakeUser, security_logs: pytest.LogCaptureFixture
) -> None:
    free = make_user()

    db_client.post(
        f"{URLS}/", json={"original_url": TARGET, "custom_alias": "mine"}, headers=free.headers
    )

    (message,) = security_messages(security_logs)
    assert "insufficient_tier" in message


def test_a_rate_limit_hit_is_logged(
    db_client: TestClient, security_logs: pytest.LogCaptureFixture
) -> None:
    for _ in range(6):
        login(db_client, "nobody@example.com")

    assert any("rate limit" in m.lower() for m in security_messages(security_logs))


def test_tokens_and_the_authorization_header_are_never_logged(
    db_client: TestClient, make_user: MakeUser, security_logs: pytest.LogCaptureFixture
) -> None:
    alice, bob = make_user(), make_user(UserTier.PREMIUM)
    code = db_client.post(f"{URLS}/", json={"original_url": TARGET}, headers=alice.headers).json()[
        "short_code"
    ]

    db_client.get(f"{URLS}/{code}/", headers=bob.headers)  # 403
    db_client.get(f"/api/v1/analytics/{code}/", headers=alice.headers)  # 403 (Free)
    db_client.get("/api/v1/auth/me/", headers={"Authorization": "Bearer not.a.real.token"})

    for user in (alice, bob):
        token = user.headers["Authorization"].removeprefix("Bearer ")
        assert token not in security_logs.text
    assert "not.a.real.token" not in security_logs.text
    assert "Authorization" not in security_logs.text
    assert "Bearer" not in security_logs.text


# ---------- clean errors ----------


def test_a_crash_shows_the_client_nothing_internal(
    app: FastAPI, db_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    @app.get("/_test/crash")
    async def crash() -> None:
        raise RuntimeError("secret detail: password=hunter2 at /srv/app/db.py")

    response = db_client.get("/_test/crash")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_server_error",
            "message": "An unexpected error occurred.",
            "details": None,
        }
    }
    assert "Traceback" not in response.text and "hunter2" not in response.text
    assert response.headers["x-request-id"]  # quote it to find the full error in the log
    assert "Traceback" in caplog.text  # the server log keeps everything


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "/health"), ("get", "/api/v1/urls/"), ("get", "/nope/path/"), ("get", "/docs")],
)
def test_every_response_has_basic_security_headers(
    db_client: TestClient, method: str, path: str
) -> None:
    response = getattr(db_client, method)(path)

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
