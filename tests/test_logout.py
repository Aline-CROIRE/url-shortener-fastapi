"""Logging out: end ONE device's session, or every session of the account at once.

POST /auth/logout/      body {"refresh_token": ...}   204
    Ends the session of that refresh token. No access token is needed: having
    the refresh token is the proof (RFC 7009), and logging out must still work
    when the 15-minute access token has already expired. The token is marked
    USED (not deleted), so a thief who kept a copy is still caught by reuse
    detection when they try it later. Calling it again is fine (204 again).
    The access token of that device is not stored anywhere, so it cannot be
    deleted: it simply runs out (at most ACCESS_TOKEN_MINUTES); the client
    forgets it.

POST /auth/logout-all/  Bearer access token              204
    Ends EVERY session: all refresh tokens are revoked and tokens_valid_after
    is set, so every access token (this one too) stops working at once.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.core.config import Settings
from app.models import RefreshToken, User

AUTH = "/api/v1/auth"
EMAIL = "aline@example.com"
OTHER = "other@example.com"
PASSWORD = "a-long-passphrase"

RunDb = Callable[..., Any]


def register_and_login(client: TestClient, email: str = EMAIL) -> dict[str, str]:
    client.post(f"{AUTH}/register/", json={"email": email, "password": PASSWORD})
    return login(client, email)


def login(client: TestClient, email: str = EMAIL) -> dict[str, str]:
    response = client.post(f"{AUTH}/login/", data={"username": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


def refresh(client: TestClient, refresh_token: str) -> Any:
    return client.post(f"{AUTH}/refresh/", json={"refresh_token": refresh_token})


def logout(client: TestClient, refresh_token: str) -> Any:
    return client.post(f"{AUTH}/logout/", json={"refresh_token": refresh_token})


def logout_all(client: TestClient, access_token: str | None) -> Any:
    headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
    return client.post(f"{AUTH}/logout-all/", headers=headers)


def me(client: TestClient, access_token: str) -> Any:
    return client.get(f"{AUTH}/me/", headers={"Authorization": f"Bearer {access_token}"})


def load_user(run_db: RunDb, email: str = EMAIL) -> User:
    user = run_db(lambda session: session.scalar(select(User).where(User.email == email)))
    assert user is not None
    return user


def stored_rows(run_db: RunDb, email: str = EMAIL) -> list[RefreshToken]:
    async def load(session: Any) -> list[RefreshToken]:
        rows = await session.scalars(select(RefreshToken).join(User).where(User.email == email))
        return list(rows)

    return run_db(load)


def error_code(response: Any) -> str:
    return response.json()["error"]["code"]


def forge(settings: Settings, key: str | None = None, **claims: Any) -> str:
    """Sign a token with chosen claims (the real key unless another one is given)."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": "1",
        "type": "refresh",
        "iat": now,
        "exp": now + timedelta(days=1),
        "jti": "forged-jti",
    }
    payload.update(claims)
    return jwt.encode(payload, key or settings.jwt_secret_key.get_secret_value(), "HS256")


def user_id(client: TestClient, access_token: str) -> str:
    return str(me(client, access_token).json()["id"])


OTHER_KEY = "another-secret-key-of-32-chars-x"


# ---------- logout: one device ----------


def test_logout_ends_this_session(db_client: TestClient) -> None:
    tokens = register_and_login(db_client)

    response = logout(db_client, tokens["refresh_token"])

    assert response.status_code == 204
    assert response.content == b""
    assert refresh(db_client, tokens["refresh_token"]).status_code == 401


def test_logout_needs_no_access_token(db_client: TestClient) -> None:
    # The access token has run out (the user comes back after an hour): logout still works
    tokens = register_and_login(db_client)

    response = db_client.post(
        f"{AUTH}/logout/",
        json={"refresh_token": tokens["refresh_token"]},
        headers={"Authorization": "Bearer expired.or.missing"},
    )

    assert response.status_code == 204


def test_logout_leaves_the_other_devices_logged_in(db_client: TestClient) -> None:
    phone = register_and_login(db_client)
    laptop = login(db_client)

    logout(db_client, phone["refresh_token"])

    assert refresh(db_client, laptop["refresh_token"]).status_code == 200
    assert me(db_client, laptop["access_token"]).status_code == 200


def test_logout_does_not_touch_other_users(db_client: TestClient) -> None:
    mine = register_and_login(db_client)
    theirs = register_and_login(db_client, OTHER)

    logout(db_client, mine["refresh_token"])

    assert refresh(db_client, theirs["refresh_token"]).status_code == 200


def test_logging_out_twice_is_fine(db_client: TestClient) -> None:
    # A double click, or a retry after a lost answer: the session is already ended
    tokens = register_and_login(db_client)
    logout(db_client, tokens["refresh_token"])

    assert logout(db_client, tokens["refresh_token"]).status_code == 204


def test_a_thief_using_a_logged_out_token_triggers_reuse_detection(
    db_client: TestClient,
) -> None:
    # The token is marked USED, not forgotten: a copy coming back is a reuse
    stolen = register_and_login(db_client)
    laptop = login(db_client)
    logout(db_client, stolen["refresh_token"])

    assert refresh(db_client, stolen["refresh_token"]).status_code == 401

    # Someone kept a copy: every session of the account is revoked, as for any reuse
    assert refresh(db_client, laptop["refresh_token"]).status_code == 401
    assert me(db_client, laptop["access_token"]).status_code == 401


def test_logout_marks_the_token_used_and_stores_no_token(
    db_client: TestClient, run_db: RunDb
) -> None:
    tokens = register_and_login(db_client)

    logout(db_client, tokens["refresh_token"])

    (row,) = stored_rows(run_db)
    assert row.used_at is not None  # marked used, not deleted: reuse detection still works
    assert row.jti != tokens["refresh_token"]  # only the id is stored, never the token


def test_logout_after_a_rotation_ends_the_newest_session(db_client: TestClient) -> None:
    first = register_and_login(db_client)
    newest = refresh(db_client, first["refresh_token"]).json()

    assert logout(db_client, newest["refresh_token"]).status_code == 204
    assert refresh(db_client, newest["refresh_token"]).status_code == 401


def test_logout_with_an_old_rotated_token_changes_nothing(db_client: TestClient) -> None:
    # A stale client sends the token it had before rotating: 204, but no alarm either
    first = register_and_login(db_client)
    newest = refresh(db_client, first["refresh_token"]).json()

    assert logout(db_client, first["refresh_token"]).status_code == 204
    assert refresh(db_client, newest["refresh_token"]).status_code == 200


def test_logout_keeps_updated_at(db_client: TestClient, run_db: RunDb) -> None:
    tokens = register_and_login(db_client)
    long_ago = datetime(2020, 1, 1, tzinfo=UTC)
    run_db(
        lambda session: session.execute(
            update(User).where(User.email == EMAIL).values(updated_at=long_ago)
        )
    )

    logout(db_client, tokens["refresh_token"])

    assert load_user(run_db).updated_at == long_ago  # logging out is not editing the account


# ---------- logout: what an attacker or a broken client may send ----------


def test_logout_refuses_an_access_token(db_client: TestClient) -> None:
    tokens = register_and_login(db_client)

    response = logout(db_client, tokens["access_token"])

    assert response.status_code == 401
    assert error_code(response) == "invalid_token"


@pytest.mark.parametrize(
    "token",
    ["not-a-jwt", "a.b.c", "x" * 2048],
    ids=["garbage", "three-parts", "longest-allowed"],
)
def test_logout_refuses_tokens_that_are_not_ours(db_client: TestClient, token: str) -> None:
    response = logout(db_client, token)

    assert response.status_code == 401
    assert error_code(response) == "invalid_token"


@pytest.mark.parametrize("kind", ["wrong_key", "expired", "alg_none", "tampered"])
def test_logout_refuses_forged_tokens(
    db_client: TestClient, test_settings: Settings, kind: str
) -> None:
    tokens = register_and_login(db_client)
    uid = user_id(db_client, tokens["access_token"])
    past = datetime.now(UTC) - timedelta(days=10)
    if kind == "wrong_key":
        token = forge(test_settings, key=OTHER_KEY, sub=uid)
    elif kind == "expired":
        token = forge(test_settings, sub=uid, iat=past, exp=past + timedelta(days=7))
    elif kind == "alg_none":
        body = {"sub": uid, "type": "refresh", "iat": 0, "exp": 4102444800, "jti": "x"}
        token = jwt.encode(body, None, algorithm="none")
    else:  # a real token with another user id pasted in, the old signature kept
        header, _payload, signature = tokens["refresh_token"].split(".")
        other = jwt.utils.base64url_encode(f'{{"sub":"{int(uid) + 1}"}}'.encode()).decode()
        token = f"{header}.{other}.{signature}"

    response = logout(db_client, token)

    assert response.status_code == 401
    assert error_code(response) == "invalid_token"
    assert refresh(db_client, tokens["refresh_token"]).status_code == 200  # nothing ended


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"refresh_token": ""},
        {"refresh_token": "x" * 2049},
        {"refresh_token": None},
        {"refresh_token": "t", "user_id": 1},
    ],
    ids=["missing", "empty", "too-long", "null", "extra-field"],
)
def test_logout_validates_the_body(db_client: TestClient, body: dict[str, Any]) -> None:
    # extra="forbid": a "user_id" cannot point the logout at another account
    response = db_client.post(f"{AUTH}/logout/", json=body)

    assert response.status_code == 422


def test_logout_of_a_token_whose_user_is_gone_is_quiet(
    db_client: TestClient, test_settings: Settings
) -> None:
    # Correctly signed, but nobody has this id or this jti: nothing to end, nothing revealed
    token = forge(test_settings, sub="2147483000")

    assert logout(db_client, token).status_code == 204


def test_logout_of_a_jti_of_another_user_does_not_end_it(
    db_client: TestClient, test_settings: Settings, run_db: RunDb
) -> None:
    # A token signed for MY id but carrying the jti of SOMEONE ELSE's session (only
    # possible with the key, but checked anyway): their session must stay alive
    mine = register_and_login(db_client)
    theirs = register_and_login(db_client, OTHER)
    (their_row,) = stored_rows(run_db, OTHER)
    token = forge(test_settings, sub=user_id(db_client, mine["access_token"]), jti=their_row.jti)

    assert logout(db_client, token).status_code == 204
    assert refresh(db_client, theirs["refresh_token"]).status_code == 200


def test_logout_answers_are_never_cached(db_client: TestClient) -> None:
    tokens = register_and_login(db_client)

    response = logout(db_client, tokens["refresh_token"])

    assert response.headers["cache-control"] == "no-store"


def test_logout_works_without_the_trailing_slash(db_client: TestClient) -> None:
    tokens = register_and_login(db_client)

    response = db_client.post(f"{AUTH}/logout", json={"refresh_token": tokens["refresh_token"]})

    assert response.status_code == 204


# ---------- logout-all: every device ----------


def test_logout_all_ends_every_session_at_once(db_client: TestClient) -> None:
    phone = register_and_login(db_client)
    laptop = login(db_client)

    assert logout_all(db_client, phone["access_token"]).status_code == 204

    # Refresh tokens AND access tokens of every device, this one included
    for device in (phone, laptop):
        assert refresh(db_client, device["refresh_token"]).status_code == 401
        assert me(db_client, device["access_token"]).status_code == 401


def test_logout_all_is_not_a_reuse_alarm(db_client: TestClient, caplog: Any) -> None:
    tokens = register_and_login(db_client)

    with caplog.at_level("WARNING", logger="app.security"):
        logout_all(db_client, tokens["access_token"])

    assert "reuse" not in caplog.text


def test_after_logout_all_logging_in_again_works(db_client: TestClient) -> None:
    tokens = register_and_login(db_client)
    logout_all(db_client, tokens["access_token"])

    again = login(db_client)

    assert me(db_client, again["access_token"]).status_code == 200
    assert refresh(db_client, again["refresh_token"]).status_code == 200


def test_logout_all_does_not_touch_other_users(db_client: TestClient) -> None:
    mine = register_and_login(db_client)
    theirs = register_and_login(db_client, OTHER)

    logout_all(db_client, mine["access_token"])

    assert me(db_client, theirs["access_token"]).status_code == 200
    assert refresh(db_client, theirs["refresh_token"]).status_code == 200


def test_logout_all_needs_an_access_token(db_client: TestClient) -> None:
    response = logout_all(db_client, None)

    assert response.status_code == 401
    assert error_code(response) == "not_authenticated"


def test_logout_all_refuses_a_refresh_token(db_client: TestClient) -> None:
    # The long-lived refresh token is never accepted as an access token
    tokens = register_and_login(db_client)

    assert logout_all(db_client, tokens["refresh_token"]).status_code == 401
    assert me(db_client, tokens["access_token"]).status_code == 200  # nothing was revoked


def test_logout_all_with_a_revoked_token_does_nothing(db_client: TestClient) -> None:
    tokens = register_and_login(db_client)
    logout_all(db_client, tokens["access_token"])
    again = login(db_client)

    # The OLD access token cannot log the new session out
    assert logout_all(db_client, tokens["access_token"]).status_code == 401
    assert me(db_client, again["access_token"]).status_code == 200


def test_logout_all_for_a_switched_off_account_is_refused(
    db_client: TestClient, run_db: RunDb
) -> None:
    tokens = register_and_login(db_client)
    run_db(
        lambda session: session.execute(
            update(User).where(User.email == EMAIL).values(is_active=False)
        )
    )

    assert logout_all(db_client, tokens["access_token"]).status_code == 401


def test_logout_all_keeps_updated_at(db_client: TestClient, run_db: RunDb) -> None:
    tokens = register_and_login(db_client)
    long_ago = datetime(2020, 1, 1, tzinfo=UTC)
    run_db(
        lambda session: session.execute(
            update(User).where(User.email == EMAIL).values(updated_at=long_ago)
        )
    )

    logout_all(db_client, tokens["access_token"])

    user = load_user(run_db)
    assert user.updated_at == long_ago
    assert user.tokens_valid_after is not None
    assert stored_rows(run_db) == []


def test_logout_all_ignores_a_body(db_client: TestClient) -> None:
    # No body is read: a "user_id" cannot point it at someone else's account
    mine = register_and_login(db_client)
    theirs = register_and_login(db_client, OTHER)
    other_id = int(user_id(db_client, theirs["access_token"]))

    db_client.post(
        f"{AUTH}/logout-all/",
        json={"user_id": other_id},
        headers={"Authorization": f"Bearer {mine['access_token']}"},
    )

    assert me(db_client, theirs["access_token"]).status_code == 200


def test_logout_all_works_without_the_trailing_slash(db_client: TestClient) -> None:
    tokens = register_and_login(db_client)

    response = db_client.post(
        f"{AUTH}/logout-all", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )

    assert response.status_code == 204


def test_a_forged_access_token_cannot_log_anyone_out(
    db_client: TestClient, test_settings: Settings
) -> None:
    victim = register_and_login(db_client)
    victim_id = user_id(db_client, victim["access_token"])

    forged = forge(test_settings, key=OTHER_KEY, sub=victim_id, type="access")

    assert logout_all(db_client, forged).status_code == 401
    assert me(db_client, victim["access_token"]).status_code == 200


# ---------- documentation ----------


def test_swagger_documents_both_logout_endpoints(db_client: TestClient) -> None:
    paths = db_client.app.openapi()["paths"]  # type: ignore[attr-defined]

    assert set(paths[f"{AUTH}/logout/"]["post"]["responses"]) >= {"204", "401", "422"}
    assert set(paths[f"{AUTH}/logout-all/"]["post"]["responses"]) >= {"204", "401"}
    assert f"{AUTH}/logout" not in paths  # the no-slash alias works but is not listed twice
