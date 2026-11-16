"""Register, log in, log out, and every URL request filtered by the logged-in user."""

import base64
import json
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from pydantic import ValidationError
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import DEV_SECRET_KEY, Settings
from app.core.security import SESSION_COOKIE, hash_password, verify_password
from app.models import User
from app.repositories import UserRepository

AUTH = "/api/v1/auth"
URLS = "/api/v1/urls"
TARGET = "https://www.python.org/about/"
PASSWORD = "a-long-password"

RunDb = Callable[..., Any]


def register(client: TestClient, username: str, password: str = PASSWORD) -> dict[str, Any]:
    response = client.post(
        f"{AUTH}/register/",
        json={"email": f"{username}@example.com", "username": username, "password": password},
    )
    assert response.status_code == 201, response.text
    return response.json()


def login(client: TestClient, username: str, password: str = PASSWORD) -> Any:
    return client.post(
        f"{AUTH}/login/", json={"email": f"{username}@example.com", "password": password}
    )


def error_code(response: Any) -> str:
    return response.json()["error"]["code"]


# ---------- register ----------


def test_register_creates_the_account(db_client: TestClient) -> None:
    body = register(db_client, "aline")

    assert body["email"] == "aline@example.com"
    assert body["username"] == "aline"
    assert body["is_active"] is True
    assert "password" not in body and "hashed_password" not in body


def test_password_is_stored_as_an_argon2_hash(db_client: TestClient, run_db: RunDb) -> None:
    register(db_client, "aline")

    stored = run_db(
        lambda session: session.scalar(select(User.hashed_password).where(User.username == "aline"))
    )

    assert stored.startswith("$argon2id$")
    assert PASSWORD not in stored
    assert verify_password(PASSWORD, stored)


@pytest.mark.parametrize(
    ("second", "field"),
    [
        ({"email": "ALINE@example.com", "username": "other"}, "email"),  # any letter case
        ({"email": "other@example.com", "username": "aline"}, "username"),
    ],
)
def test_register_twice_is_409_with_the_field(
    db_client: TestClient, second: dict[str, str], field: str
) -> None:
    register(db_client, "aline")

    response = db_client.post(f"{AUTH}/register/", json={**second, "password": PASSWORD})

    assert response.status_code == 409
    assert error_code(response) == "conflict"
    assert response.json()["error"]["details"] == {"field": field}


def test_other_unique_violations_during_register_stay_generic(
    db_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class UniqueViolation(Exception):
        sqlstate = "23505"

    cause = Exception()
    cause.constraint_name = "uq_something_else"  # type: ignore[attr-defined]
    original = UniqueViolation()
    original.__cause__ = cause

    async def clash(self: Any, user: User) -> User:
        raise IntegrityError("INSERT INTO users ...", {}, original)

    monkeypatch.setattr(UserRepository, "add", clash)

    response = db_client.post(
        f"{AUTH}/register/",
        json={"email": "a@example.com", "username": "aline", "password": PASSWORD},
    )

    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"constraint": "uq_something_else"}


@pytest.mark.parametrize(
    "body",
    [
        {"email": "a@example.com", "username": "aline", "password": "short"},
        {"email": "not-an-email", "username": "aline", "password": PASSWORD},
        {"email": "a@example.com", "username": "a", "password": PASSWORD},
    ],
)
def test_register_rejects_bad_input(db_client: TestClient, body: dict[str, str]) -> None:
    assert db_client.post(f"{AUTH}/register/", json=body).status_code == 422


# ---------- login / logout / me ----------


def test_login_sets_an_http_only_session_cookie(db_client: TestClient, run_db: RunDb) -> None:
    register(db_client, "aline")

    response = login(db_client, "aline")

    assert response.status_code == 200
    assert response.json()["username"] == "aline"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    last_login = run_db(
        lambda session: session.scalar(select(User.last_login).where(User.username == "aline"))
    )
    assert last_login is not None


def test_login_email_is_not_case_sensitive(db_client: TestClient) -> None:
    register(db_client, "aline")

    response = db_client.post(
        f"{AUTH}/login/", json={"email": "ALINE@Example.com", "password": PASSWORD}
    )

    assert response.status_code == 200


def test_wrong_password_unknown_email_and_inactive_user_get_the_same_401(
    db_client: TestClient, run_db: RunDb
) -> None:
    register(db_client, "aline")
    register(db_client, "sleeper")
    run_db(
        lambda session: session.execute(
            update(User).where(User.username == "sleeper").values(is_active=False)
        )
    )

    responses = [
        login(db_client, "aline", password="wrong-password"),
        login(db_client, "nobody"),
        login(db_client, "sleeper"),
    ]

    # Same answer every time: nobody can find out which emails are registered
    assert {r.status_code for r in responses} == {401}
    assert {r.json()["error"]["message"] for r in responses} == {"The email or password is wrong."}
    assert all(error_code(r) == "invalid_credentials" for r in responses)
    assert all("set-cookie" not in r.headers for r in responses)


def test_the_real_app_without_test_overrides_asks_for_login(client: TestClient) -> None:
    # "client" uses the app exactly as in production (its own session factory)
    response = client.get(f"{AUTH}/me/")

    assert response.status_code == 401
    assert error_code(response) == "not_authenticated"


def test_me_shows_the_logged_in_user(db_client: TestClient) -> None:
    register(db_client, "aline")
    login(db_client, "aline")

    response = db_client.get(f"{AUTH}/me/")

    assert response.status_code == 200
    assert response.json()["username"] == "aline"


def test_without_login_the_private_endpoints_are_401(db_client: TestClient) -> None:
    for response in (
        db_client.get(f"{AUTH}/me/"),
        db_client.get(f"{URLS}/"),
        db_client.post(f"{URLS}/", json={"original_url": TARGET}),
        db_client.get(f"{URLS}/abcdefg/"),
        db_client.put(f"{URLS}/abcdefg/", json={"is_active": False}),
        db_client.delete(f"{URLS}/abcdefg/"),
        db_client.get("/api/v1/analytics/abcdefg/"),
    ):
        assert response.status_code == 401, response.request.url
        assert error_code(response) == "not_authenticated"


def test_logout_ends_the_session(db_client: TestClient) -> None:
    register(db_client, "aline")
    login(db_client, "aline")
    assert db_client.get(f"{URLS}/").status_code == 200

    assert db_client.post(f"{AUTH}/logout/").status_code == 204

    assert db_client.get(f"{URLS}/").status_code == 401
    assert db_client.post(f"{AUTH}/logout/").status_code == 204  # harmless twice


def test_a_changed_cookie_is_not_accepted(db_client: TestClient) -> None:
    register(db_client, "aline")
    login(db_client, "aline")
    real = db_client.cookies[SESSION_COOKIE]

    # Someone edits the cookie (e.g. to pretend to be user 1): the signature no longer matches
    db_client.cookies.set(SESSION_COOKIE, "x" + real[1:])

    assert db_client.get(f"{AUTH}/me/").status_code == 401


def test_a_cookie_signed_with_another_secret_is_not_accepted(
    db_client: TestClient,
) -> None:

    register(db_client, "aline")
    payload = base64.b64encode(json.dumps({"user_id": 1}).encode())
    forged = TimestampSigner("not-the-real-secret").sign(payload).decode()
    db_client.cookies.set(SESSION_COOKIE, forged)

    assert db_client.get(f"{AUTH}/me/").status_code == 401


@pytest.mark.parametrize("change", ["deactivate", "delete"])
def test_a_deactivated_or_deleted_user_is_logged_out(
    db_client: TestClient, run_db: RunDb, change: str
) -> None:
    register(db_client, "aline")
    login(db_client, "aline")

    async def apply(session: AsyncSession) -> None:
        if change == "deactivate":
            await session.execute(
                update(User).where(User.username == "aline").values(is_active=False)
            )
        else:
            await session.execute(delete(User).where(User.username == "aline"))

    run_db(apply)

    response = db_client.get(f"{AUTH}/me/")
    assert response.status_code == 401
    assert "session=null" in response.headers["set-cookie"]  # the cookie is cleared too


def test_aliases_without_the_trailing_slash_work(db_client: TestClient) -> None:
    response = db_client.post(
        f"{AUTH}/register",
        json={"email": "aline@example.com", "username": "aline", "password": PASSWORD},
    )
    assert response.status_code == 201
    credentials = {"email": "aline@example.com", "password": PASSWORD}
    assert db_client.post(f"{AUTH}/login", json=credentials).status_code == 200
    assert db_client.get(f"{AUTH}/me").status_code == 200
    assert db_client.post(f"{AUTH}/logout").status_code == 204


# ---------- every request is filtered by the logged-in user ----------


def test_users_only_see_and_change_their_own_urls(db_client: TestClient) -> None:
    register(db_client, "aline")
    register(db_client, "bob")

    login(db_client, "aline")
    code = db_client.post(f"{URLS}/", json={"original_url": TARGET}).json()["short_code"]
    assert [u["short_code"] for u in db_client.get(f"{URLS}/").json()["items"]] == [code]

    login(db_client, "bob")  # logging in as someone else replaces the session
    assert db_client.get(f"{AUTH}/me/").json()["username"] == "bob"
    assert db_client.get(f"{URLS}/").json()["items"] == []
    # Aline's link does not exist for Bob (404, not 403: he cannot even learn it exists)
    assert db_client.get(f"{URLS}/{code}/").status_code == 404
    assert db_client.put(f"{URLS}/{code}/", json={"is_active": False}).status_code == 404
    assert db_client.delete(f"{URLS}/{code}/").status_code == 404
    assert db_client.get(f"/api/v1/analytics/{code}/").status_code == 404

    login(db_client, "aline")
    body = db_client.get(f"{URLS}/{code}/").json()
    assert body["is_active"] is True  # Bob's attempts changed nothing
    assert body["owner"]["username"] == "aline"


def test_redirect_and_health_stay_public(db_client: TestClient) -> None:
    register(db_client, "aline")
    login(db_client, "aline")
    code = db_client.post(f"{URLS}/", json={"original_url": TARGET}).json()["short_code"]
    db_client.post(f"{AUTH}/logout/")

    assert db_client.get(f"/{code}").status_code == 302  # anyone can follow a short link
    assert db_client.get("/health").status_code == 200


# ---------- settings and hashing ----------


def test_production_refuses_the_development_secret_key() -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(environment="production", secret_key=DEV_SECRET_KEY)
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        Settings(environment="production", secret_key="too-short")

    ok = Settings(environment="production", secret_key="x" * 40)
    assert ok.secret_key == "x" * 40


def test_verify_password() -> None:
    hashed = hash_password(PASSWORD)

    assert verify_password(PASSWORD, hashed)
    assert not verify_password("wrong-password", hashed)
    assert not verify_password(PASSWORD, "not-a-hash")  # a broken hash is just "no"
