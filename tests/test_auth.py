"""Authentication: register, log in (OAuth2 password form), refresh, and Bearer tokens.

Written BEFORE the code, from three points of view:
- the user: the normal paths work and every error is clear
- the attacker: forged, expired, swapped or replayed tokens, extra fields, huge or
  look-alike input, guessing which emails exist; all must fail closed (401/422, never 500)
- the reviewer (Module 6 lessons): no hidden writes (a login must not change
  updated_at) and no stale trust (tier and is_active come from the database on
  every request, never from the token)
"""

import base64
import logging
import re
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

import app.services.auth as auth_service_module
from app.core.config import Settings
from app.core.security import DUMMY_HASH, verify_password
from app.main import app as real_app
from app.models import User, UserTier
from app.repositories import UserRepository
from app.schemas.user import USERNAME_PATTERN

AUTH = "/api/v1/auth"
URLS = "/api/v1/urls"
TARGET = "https://www.python.org/about/"
EMAIL = "aline@example.com"
PASSWORD = "a-long-passphrase"

RunDb = Callable[..., Any]

# A "Basic" login header, built at run time from obviously fake text: no
# credential-shaped string is written in the code (secret scanners flag those)
FAKE_BASIC_AUTH = "Basic " + base64.b64encode(b"nobody:not-a-real-password").decode()


# ---------- helpers ----------


def register(
    client: TestClient, email: str = EMAIL, password: str = PASSWORD, **extra: Any
) -> httpx.Response:
    return client.post(f"{AUTH}/register/", json={"email": email, "password": password, **extra})


def login(client: TestClient, email: str = EMAIL, password: str = PASSWORD) -> httpx.Response:
    # The OAuth2 password flow: a FORM (not JSON), and the email goes in "username"
    return client.post(f"{AUTH}/login/", data={"username": email, "password": password})


def get_tokens(client: TestClient, email: str = EMAIL) -> dict[str, str]:
    """Register and log in; return the token pair."""
    assert register(client, email).status_code == 201
    response = login(client, email)
    assert response.status_code == 200, response.text
    return response.json()


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def refresh(client: TestClient, token: str) -> httpx.Response:
    return client.post(f"{AUTH}/refresh/", json={"refresh_token": token})


def me(client: TestClient, token: str) -> httpx.Response:
    return client.get(f"{AUTH}/me/", headers=bearer(token))


def error_code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]


def claims(token: str) -> dict[str, Any]:
    """Read a token's payload WITHOUT checking it (only to look inside in tests)."""
    return jwt.decode(token, options={"verify_signature": False})


def forge(settings: Settings, *, key: str | None = None, **overrides: Any) -> str:
    """Sign a token with chosen claims (the real key unless another one is given)."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": "1",
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "jti": "test-jti",
    }
    payload.update(overrides)
    return jwt.encode(payload, key or settings.jwt_secret_key.get_secret_value(), "HS256")


def user_id(run_db: RunDb, email: str = EMAIL) -> int:
    return run_db(lambda session: session.scalar(select(User.id).where(User.email == email)))


def load_user(run_db: RunDb, email: str = EMAIL) -> User | None:
    return run_db(lambda session: session.scalar(select(User).where(User.email == email)))


def change_user(run_db: RunDb, email: str = EMAIL, **values: Any) -> None:
    run_db(lambda session: session.execute(update(User).where(User.email == email).values(values)))


def delete_user(run_db: RunDb, email: str = EMAIL) -> None:
    run_db(lambda session: session.execute(delete(User).where(User.email == email)))


def count_users(run_db: RunDb, email: str) -> int:
    return run_db(lambda session: session.scalar(select(func.count()).where(User.email == email)))


def bad_token(kind: str, settings: Settings, token_type: str, real: str, run_db: RunDb) -> str:
    """Build one kind of bad token for the parametrized attack tests."""
    uid = str(user_id(run_db))
    past = datetime.now(UTC) - timedelta(hours=1)
    if kind == "expired":
        return forge(settings, sub=uid, type=token_type, iat=past, exp=past + timedelta(minutes=1))
    if kind == "wrong_key":
        return forge(settings, sub=uid, type=token_type, key="another-secret-key-of-32-chars-x")
    if kind == "alg_none":  # unsigned token: the classic "alg: none" attack
        body = {"sub": uid, "type": token_type, "iat": 0, "exp": 4102444800, "jti": "x"}
        return jwt.encode(body, None, algorithm="none")
    if kind == "tampered":  # change the user id inside a REAL token, keep the old signature
        header, _payload, signature = real.split(".")
        other = jwt.utils.base64url_encode(
            f'{{"sub":"{int(uid) + 1}","type":"{token_type}"}}'.encode()
        ).decode()
        return f"{header}.{other}.{signature}"
    if kind == "unknown_user":  # correctly signed, but nobody has this id
        return forge(settings, sub="2147483000", type=token_type)
    if kind == "no_jti":
        return jwt.encode(
            {"sub": uid, "type": token_type, "iat": datetime.now(UTC), "exp": 4102444800},
            settings.jwt_secret_key.get_secret_value(),
            "HS256",
        )
    return "not.a.jwt"  # garbage


BAD_TOKEN_KINDS = [
    "expired",
    "wrong_key",
    "alg_none",
    "tampered",
    "unknown_user",
    "no_jti",
    "garbage",
]


# ---------- register ----------


def test_register_returns_201_with_only_id_email_and_tier(db_client: TestClient) -> None:
    response = register(db_client, "Aline@Example.COM")

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "email", "tier"}
    assert isinstance(body["id"], int)
    assert body["email"] == EMAIL  # stored lower-case
    assert body["tier"] == "free"
    assert "set-cookie" not in response.headers  # no cookie sessions any more


def test_register_stores_only_an_argon2id_hash(db_client: TestClient, run_db: RunDb) -> None:
    register(db_client)

    user = load_user(run_db)

    assert user is not None
    assert user.hashed_password.startswith("$argon2id$")
    assert PASSWORD not in user.hashed_password
    assert verify_password(PASSWORD, user.hashed_password)


@pytest.mark.parametrize("again", [EMAIL, "ALINE@EXAMPLE.COM", "  aline@example.com  "])
def test_registering_the_same_email_again_is_409(
    db_client: TestClient, run_db: RunDb, again: str
) -> None:
    register(db_client)

    response = register(db_client, again)

    assert response.status_code == 409
    assert error_code(response) == "conflict"
    assert response.json()["error"]["details"] == {"field": "email"}
    assert count_users(run_db, EMAIL) == 1


def test_a_chosen_username_that_is_taken_is_409(db_client: TestClient) -> None:
    register(db_client, username="aline")

    response = register(db_client, "other@example.com", username="aline")

    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"field": "username"}


def test_without_a_username_one_is_made_from_the_email(
    db_client: TestClient, run_db: RunDb
) -> None:
    register(db_client, "Aline.Smith@example.com")

    user = load_user(run_db, "aline.smith@example.com")

    assert user is not None
    assert user.username == "aline.smith"


def test_a_made_username_gets_a_number_when_it_is_taken(
    db_client: TestClient, run_db: RunDb
) -> None:
    # Same name before the @, different domains: both must get an account (never 409)
    assert register(db_client, "aline@first.com").status_code == 201
    assert register(db_client, "aline@second.com").status_code == 201

    first = load_user(run_db, "aline@first.com")
    second = load_user(run_db, "aline@second.com")

    assert first is not None and second is not None
    assert first.username == "aline"
    assert second.username != "aline"
    assert second.username.startswith("aline")
    assert second.username.removeprefix("aline").isdigit()


@pytest.mark.parametrize(
    "email",
    [
        "a+promo@example.com",  # "+" is allowed in emails but not in usernames
        "ab@example.com",  # too short for a username (3 at least)
        "x@example.com",
        "o'brien@example.com",
        "_@example.com",
        f"{'a' * 64}@example.com",  # the longest local part
    ],
)
def test_a_made_username_is_always_a_valid_username(
    db_client: TestClient, run_db: RunDb, email: str
) -> None:
    assert register(db_client, email).status_code == 201

    user = load_user(run_db, email)

    assert user is not None
    assert 3 <= len(user.username) <= 150
    assert re.fullmatch(USERNAME_PATTERN, user.username)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tier", "admin"),  # make myself admin
        ("tier", "premium"),
        ("is_active", True),
        ("is_premium", True),
        ("id", 1),  # take over someone's id
        ("hashed_password", "$argon2id$fake"),
        ("last_login", "2020-01-01T00:00:00Z"),
        ("created_at", "2020-01-01T00:00:00Z"),
    ],
)
def test_register_refuses_every_field_it_does_not_expect(
    db_client: TestClient, run_db: RunDb, field: str, value: Any
) -> None:
    response = register(db_client, **{field: value})

    assert response.status_code == 422
    assert count_users(run_db, EMAIL) == 0  # nothing was created


@pytest.mark.parametrize(
    "body",
    [
        {"email": EMAIL, "password": "x" * 7},  # too short
        {"email": EMAIL, "password": "x" * 129},  # too long (also stops slow hashing)
        {"email": EMAIL, "password": " " * 12},  # only spaces
        {"email": EMAIL, "password": 12345678},  # not text
        {"email": EMAIL},
        {"password": PASSWORD},
        {"email": None, "password": PASSWORD},
        {"email": "not-an-email", "password": PASSWORD},
        {"email": "aline@localhost", "password": PASSWORD},
        # a look-alike: Cyrillic "a", so it reads as aline@example.com but is another account
        {"email": "\N{CYRILLIC SMALL LETTER A}line@example.com", "password": PASSWORD},
        {"email": "jos\N{LATIN SMALL LETTER E WITH ACUTE}@example.com", "password": PASSWORD},
        {"email": EMAIL, "password": PASSWORD, "username": "a!"},
        {"email": EMAIL, "password": PASSWORD, "username": "ab"},
        {"email": EMAIL, "password": PASSWORD, "username": "u" * 151},
        {"email": EMAIL, "password": PASSWORD, "username": "aline\x00"},
    ],
)
def test_register_rejects_bad_input_with_422(db_client: TestClient, body: dict[str, Any]) -> None:
    response = db_client.post(f"{AUTH}/register/", json=body)

    assert response.status_code == 422
    assert error_code(response) == "validation_error"


@pytest.mark.parametrize("length", [8, 128])
def test_register_accepts_passwords_of_8_and_128_characters(
    db_client: TestClient, length: int
) -> None:
    assert register(db_client, password="p" * length).status_code == 201


def test_register_needs_a_json_body(db_client: TestClient) -> None:
    form = db_client.post(f"{AUTH}/register/", data={"email": EMAIL, "password": PASSWORD})
    broken = db_client.post(
        f"{AUTH}/register/", content=b"{not json", headers={"Content-Type": "application/json"}
    )

    assert form.status_code == 422
    assert broken.status_code == 422


def test_validation_errors_never_echo_the_password(db_client: TestClient) -> None:
    too_short = register(db_client, password="short12")
    with_extra = register(db_client, tier="admin")

    assert too_short.status_code == with_extra.status_code == 422
    assert "short12" not in too_short.text
    assert PASSWORD not in with_extra.text


def test_another_unique_violation_on_register_is_still_a_clean_409(
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

    response = register(db_client)

    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"constraint": "uq_something_else"}


# ---------- login ----------


def test_login_returns_a_bearer_token_pair(db_client: TestClient, run_db: RunDb) -> None:
    register(db_client)

    response = login(db_client)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"access_token", "refresh_token", "token_type"}
    assert body["token_type"] == "bearer"
    uid = str(user_id(run_db))
    assert (claims(body["access_token"])["sub"], claims(body["access_token"])["type"]) == (
        uid,
        "access",
    )
    assert (claims(body["refresh_token"])["sub"], claims(body["refresh_token"])["type"]) == (
        uid,
        "refresh",
    )
    # RFC 6749 5.1: token responses must never be stored by a browser or proxy cache
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert "set-cookie" not in response.headers


def test_the_token_carries_no_personal_data_or_tier(db_client: TestClient) -> None:
    access = get_tokens(db_client)["access_token"]

    assert set(claims(access)) == {"sub", "type", "iat", "exp", "jti"}


def test_login_ignores_letter_case_and_spaces_around_the_email(db_client: TestClient) -> None:
    register(db_client)

    assert login(db_client, "  ALINE@Example.COM ").status_code == 200


def test_every_failed_login_looks_exactly_the_same(db_client: TestClient, run_db: RunDb) -> None:
    register(db_client)
    register(db_client, "sleeper@example.com")
    change_user(run_db, "sleeper@example.com", is_active=False)

    responses = [
        login(db_client, password="wrong-password"),  # wrong password
        login(db_client, "nobody@example.com"),  # unknown email
        login(db_client, "sleeper@example.com"),  # switched-off account, right password
        login(db_client, "aline"),  # a username instead of the email
        login(db_client, "not an email at all"),
    ]

    # One identical answer: nobody can find out which emails are registered
    assert {r.status_code for r in responses} == {401}
    assert all(r.json() == responses[0].json() for r in responses)
    assert error_code(responses[0]) == "invalid_credentials"
    assert all(r.headers["www-authenticate"] == "Bearer" for r in responses)
    assert all("access_token" not in r.text for r in responses)


def test_an_unknown_email_still_runs_one_password_check(
    db_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    checked: list[str] = []

    def spy(password: str, hashed: str) -> bool:
        checked.append(hashed)
        return False

    monkeypatch.setattr(auth_service_module, "verify_password", spy)

    login(db_client, "nobody@example.com")

    # Same slow work as for a real account, so the response time gives nothing away
    assert checked == [DUMMY_HASH]


def test_a_huge_password_is_refused_without_hashing_it(
    db_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(db_client)
    calls: list[str] = []
    monkeypatch.setattr(auth_service_module, "verify_password", lambda p, h: calls.append(h))

    # Far longer than any valid password (128), but small enough to pass the body limit
    response = login(db_client, password="x" * 10_000)
    # So big that the body-size limit refuses it before any code even reads it
    enormous = login(db_client, password="x" * 100_000)

    assert response.status_code == 401
    assert error_code(response) == "invalid_credentials"
    assert enormous.status_code == 413
    assert calls == []  # never fed to the slow hash: no cheap way to load the server


def test_login_needs_the_oauth2_form(db_client: TestClient) -> None:
    register(db_client)

    as_json = db_client.post(f"{AUTH}/login/", json={"username": EMAIL, "password": PASSWORD})
    no_password = db_client.post(f"{AUTH}/login/", data={"username": EMAIL})

    assert as_json.status_code == 422
    assert no_password.status_code == 422


def test_login_records_last_login_without_changing_updated_at(
    db_client: TestClient, run_db: RunDb
) -> None:
    register(db_client)
    # The test runs in ONE transaction, where now() never moves, so start from an old
    # updated_at: if login touched it, it would change
    long_ago = datetime(2020, 1, 1, tzinfo=UTC)
    change_user(run_db, updated_at=long_ago)

    login(db_client)

    user = load_user(run_db)
    assert user is not None
    assert user.last_login is not None
    assert user.updated_at == long_ago  # logging in is not editing the account


def test_a_failed_login_does_not_record_last_login(db_client: TestClient, run_db: RunDb) -> None:
    register(db_client)

    login(db_client, password="wrong-password")

    user = load_user(run_db)
    assert user is not None
    assert user.last_login is None


def test_the_password_is_used_exactly_as_typed(db_client: TestClient) -> None:
    register(db_client, password="  spaced pass  ")

    assert login(db_client, password="spaced pass").status_code == 401  # never trimmed
    assert login(db_client, password="  spaced pass  ").status_code == 200


def test_the_same_accented_password_works_from_any_keyboard(db_client: TestClient) -> None:
    # "é" can be sent as ONE character (Windows) or as "e" + an accent mark (macOS);
    # both are normalised (NFKC) before hashing, so both log in
    composed = unicodedata.normalize("NFC", "pass-word-é")
    decomposed = unicodedata.normalize("NFD", composed)
    assert composed != decomposed
    register(db_client, password=composed)

    assert login(db_client, password=decomposed).status_code == 200


# ---------- refresh ----------


def test_refresh_returns_a_new_working_token_pair(db_client: TestClient) -> None:
    old = get_tokens(db_client)

    response = refresh(db_client, old["refresh_token"])

    assert response.status_code == 200
    new = response.json()
    assert set(new) == {"access_token", "refresh_token", "token_type"}
    assert new["access_token"] != old["access_token"]
    assert new["refresh_token"] != old["refresh_token"]
    assert response.headers["cache-control"] == "no-store"
    assert me(db_client, new["access_token"]).status_code == 200


def test_refresh_refuses_an_access_token(db_client: TestClient) -> None:
    access = get_tokens(db_client)["access_token"]

    response = refresh(db_client, access)

    assert response.status_code == 401
    assert error_code(response) == "invalid_token"
    assert response.headers["www-authenticate"] == 'Bearer error="invalid_token"'


@pytest.mark.parametrize("kind", BAD_TOKEN_KINDS)
def test_refresh_refuses_every_bad_token(
    db_client: TestClient, run_db: RunDb, test_settings: Settings, kind: str
) -> None:
    real = get_tokens(db_client)["refresh_token"]

    response = refresh(db_client, bad_token(kind, test_settings, "refresh", real, run_db))

    assert response.status_code == 401
    assert error_code(response) == "invalid_token"


@pytest.mark.parametrize("change", ["deactivate", "delete"])
def test_refresh_stops_working_for_a_switched_off_or_deleted_user(
    db_client: TestClient, run_db: RunDb, change: str
) -> None:
    tokens = get_tokens(db_client)
    if change == "deactivate":
        change_user(run_db, is_active=False)
    else:
        delete_user(run_db)

    response = refresh(db_client, tokens["refresh_token"])

    # Otherwise a blocked user could keep making new access tokens for 7 days
    assert response.status_code == 401
    assert error_code(response) == "invalid_token"


@pytest.mark.parametrize(
    "body", [{}, {"refresh_token": ""}, {"refresh_token": "x" * 5000}, {"token": "x"}]
)
def test_refresh_rejects_a_bad_body(db_client: TestClient, body: dict[str, Any]) -> None:
    assert db_client.post(f"{AUTH}/refresh/", json=body).status_code == 422


def test_refresh_needs_the_token_in_the_body_not_the_header(db_client: TestClient) -> None:
    tokens = get_tokens(db_client)

    response = db_client.post(f"{AUTH}/refresh/", headers=bearer(tokens["refresh_token"]))

    assert response.status_code == 422


# ---------- Bearer access tokens (GET /auth/me/) ----------


def test_me_shows_the_user_of_the_token(db_client: TestClient) -> None:
    access = get_tokens(db_client)["access_token"]

    response = me(db_client, access)

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == EMAIL
    assert body["tier"] == "free"
    assert body["is_premium"] is False
    assert "hashed_password" not in body and "password" not in body


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": FAKE_BASIC_AUTH},  # another login scheme, not Bearer
        {"Authorization": "Token abc.def.ghi"},
        {"Authorization": "Bearer " + "x" * 10_000},
    ],
)
def test_a_missing_or_malformed_authorization_header_is_401(
    db_client: TestClient, headers: dict[str, str]
) -> None:
    response = db_client.get(f"{AUTH}/me/", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_without_a_token_the_error_says_log_in(db_client: TestClient) -> None:
    response = db_client.get(f"{AUTH}/me/")

    assert error_code(response) == "not_authenticated"
    assert response.headers["www-authenticate"] == "Bearer"


def test_the_bearer_word_is_not_case_sensitive(db_client: TestClient) -> None:
    access = get_tokens(db_client)["access_token"]

    # RFC 7235: the scheme name is case-insensitive
    response = db_client.get(f"{AUTH}/me/", headers={"Authorization": f"bearer {access}"})

    assert response.status_code == 200


def test_a_refresh_token_is_not_an_access_token(db_client: TestClient) -> None:
    tokens = get_tokens(db_client)

    response = me(db_client, tokens["refresh_token"])

    assert response.status_code == 401
    assert error_code(response) == "invalid_token"
    assert response.headers["www-authenticate"] == 'Bearer error="invalid_token"'


@pytest.mark.parametrize("kind", BAD_TOKEN_KINDS)
def test_every_bad_access_token_is_401(
    db_client: TestClient, run_db: RunDb, test_settings: Settings, kind: str
) -> None:
    real = get_tokens(db_client)["access_token"]

    response = me(db_client, bad_token(kind, test_settings, "access", real, run_db))

    assert response.status_code == 401
    assert error_code(response) == "invalid_token"


def test_a_token_in_the_url_is_ignored(db_client: TestClient) -> None:
    access = get_tokens(db_client)["access_token"]

    # Tokens in URLs end up in logs and browser history: only the header counts
    response = db_client.get(f"{AUTH}/me/", params={"access_token": access})

    assert response.status_code == 401


def test_an_old_session_cookie_is_ignored(db_client: TestClient) -> None:
    get_tokens(db_client)
    db_client.cookies.set("session", "anything")  # what the old cookie login used

    response = db_client.get(f"{AUTH}/me/")

    assert response.status_code == 401


@pytest.mark.parametrize("change", ["deactivate", "delete"])
def test_a_token_stops_working_when_the_user_is_switched_off_or_deleted(
    db_client: TestClient, run_db: RunDb, change: str
) -> None:
    access = get_tokens(db_client)["access_token"]
    if change == "deactivate":
        change_user(run_db, is_active=False)
    else:
        delete_user(run_db)

    response = me(db_client, access)

    assert response.status_code == 401
    assert error_code(response) == "invalid_token"


def test_a_tier_change_counts_at_once_with_the_same_token(
    db_client: TestClient, run_db: RunDb
) -> None:
    access = get_tokens(db_client)["access_token"]

    change_user(run_db, tier=UserTier.PREMIUM)  # promoted directly in the database

    body = me(db_client, access).json()
    assert (body["tier"], body["is_premium"]) == ("premium", True)  # read fresh, not from the token


def test_error_bodies_never_echo_the_token(db_client: TestClient, test_settings: Settings) -> None:
    forged = forge(test_settings, key="another-secret-key-of-32-chars-x")

    response = me(db_client, forged)

    assert response.status_code == 401
    assert forged not in response.text


# ---------- the URL endpoints use the same tokens ----------


def test_without_a_token_every_private_endpoint_is_401(db_client: TestClient) -> None:
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


def test_users_only_see_and_change_their_own_urls(db_client: TestClient) -> None:
    aline = get_tokens(db_client)["access_token"]
    bob = get_tokens(db_client, "bob@example.com")["access_token"]

    created = db_client.post(f"{URLS}/", json={"original_url": TARGET}, headers=bearer(aline))
    assert created.status_code == 201
    code = created.json()["short_code"]

    assert db_client.get(f"{URLS}/", headers=bearer(bob)).json()["items"] == []
    for response in (
        db_client.get(f"{URLS}/{code}/", headers=bearer(bob)),
        db_client.put(f"{URLS}/{code}/", json={"is_active": False}, headers=bearer(bob)),
        db_client.delete(f"{URLS}/{code}/", headers=bearer(bob)),
    ):
        assert response.status_code in (403, 404)  # Phase 2 makes this exactly 403

    body = db_client.get(f"{URLS}/{code}/", headers=bearer(aline)).json()
    assert body["is_active"] is True  # Bob's attempts changed nothing


def test_redirect_and_health_stay_public(db_client: TestClient) -> None:
    access = get_tokens(db_client)["access_token"]
    code = db_client.post(f"{URLS}/", json={"original_url": TARGET}, headers=bearer(access)).json()[
        "short_code"
    ]

    assert db_client.get(f"/{code}").status_code == 302  # no token needed to follow a link
    assert db_client.get("/health").status_code == 200


def test_the_real_app_without_test_overrides_asks_for_a_token(client: TestClient) -> None:
    # "client" is the app exactly as in production (its own database sessions)
    response = client.get(f"{AUTH}/me/")

    assert response.status_code == 401
    assert error_code(response) == "not_authenticated"


# ---------- the cookie sessions are gone; Swagger shows OAuth2 ----------


def test_logout_no_longer_exists(db_client: TestClient) -> None:
    # Stateless tokens cannot be "logged out" on the server; the client forgets them
    assert db_client.post(f"{AUTH}/logout/").status_code in (404, 405)


def test_the_cookie_settings_are_gone() -> None:
    fields = set(Settings.model_fields)

    assert not {"secret_key", "session_max_age_seconds", "session_https_only"} & fields


def test_swagger_uses_the_oauth2_password_flow() -> None:
    schema = real_app.openapi()
    schemes = schema["components"]["securitySchemes"]

    assert [s["type"] for s in schemes.values()] == ["oauth2"]
    (scheme,) = schemes.values()
    assert scheme["flows"]["password"]["tokenUrl"] == "/api/v1/auth/login/"
    assert "/api/v1/auth/logout/" not in schema["paths"]
    assert {"/api/v1/auth/register/", "/api/v1/auth/login/", "/api/v1/auth/refresh/"} <= set(
        schema["paths"]
    )


def test_paths_without_the_trailing_slash_work(db_client: TestClient) -> None:
    assert (
        db_client.post(f"{AUTH}/register", json={"email": EMAIL, "password": PASSWORD}).status_code
        == 201
    )
    tokens = db_client.post(f"{AUTH}/login", data={"username": EMAIL, "password": PASSWORD}).json()
    assert (
        db_client.post(
            f"{AUTH}/refresh", json={"refresh_token": tokens["refresh_token"]}
        ).status_code
        == 200
    )
    assert db_client.get(f"{AUTH}/me", headers=bearer(tokens["access_token"])).status_code == 200


# ---------- nothing secret reaches the logs ----------


def test_passwords_and_tokens_never_reach_the_logs(
    db_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    wrong = "wrong-password-123"

    tokens = get_tokens(db_client)
    login(db_client, password=wrong)
    new = refresh(db_client, tokens["refresh_token"]).json()
    me(db_client, new["access_token"])

    secrets_sent = [PASSWORD, wrong, *tokens.values(), *new.values()]
    for secret in secrets_sent:
        if secret != "bearer":
            assert secret not in caplog.text
