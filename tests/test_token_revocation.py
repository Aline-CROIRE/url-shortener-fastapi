"""Instant revocation: every token of a user can be killed at once, before it expires.

users.tokens_valid_after is a moment in time. A token issued BEFORE it (its "iat"
claim) is refused, access and refresh alike. get_current_user loads the user on
every request anyway, so this check costs no extra query.

It is set when a refresh token is REUSED (someone kept a copy): the thief's access
token stops working at once, not 15 minutes later.

Both times are EXACT (fractions of a second, from the app's clock): a token issued
a moment before the revocation is refused even in the same second, and a login a
moment after it works.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.models import User

AUTH = "/api/v1/auth"
EMAIL = "aline@example.com"
PASSWORD = "a-long-passphrase"

RunDb = Callable[..., Any]


def register_and_login(client: TestClient, email: str = EMAIL) -> dict[str, str]:
    client.post(f"{AUTH}/register/", json={"email": email, "password": PASSWORD})
    return login(client, email)


def login(client: TestClient, email: str = EMAIL) -> dict[str, str]:
    response = client.post(f"{AUTH}/login/", data={"username": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


def me(client: TestClient, access_token: str) -> Any:
    return client.get(f"{AUTH}/me/", headers={"Authorization": f"Bearer {access_token}"})


def refresh(client: TestClient, refresh_token: str) -> Any:
    return client.post(f"{AUTH}/refresh/", json={"refresh_token": refresh_token})


def load_user(run_db: RunDb, email: str = EMAIL) -> User:
    user = run_db(lambda session: session.scalar(select(User).where(User.email == email)))
    assert user is not None
    return user


def test_a_new_user_has_no_revocation(db_client: TestClient, run_db: RunDb) -> None:
    register_and_login(db_client)

    assert load_user(run_db).tokens_valid_after is None


def test_refresh_reuse_kills_the_access_tokens_at_once(db_client: TestClient) -> None:
    stolen = register_and_login(db_client)
    refresh(db_client, stolen["refresh_token"])  # the real user refreshes

    refresh(db_client, stolen["refresh_token"])  # the copy comes back: reuse detected

    # Not 15 minutes later: right now
    response = me(db_client, stolen["access_token"])
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


def test_every_device_is_logged_out_by_a_reuse(db_client: TestClient) -> None:
    phone = register_and_login(db_client)
    laptop = login(db_client)
    refresh(db_client, phone["refresh_token"])

    refresh(db_client, phone["refresh_token"])  # reuse

    assert me(db_client, laptop["access_token"]).status_code == 401
    assert refresh(db_client, laptop["refresh_token"]).status_code == 401


def test_logging_in_right_after_a_revocation_works(db_client: TestClient) -> None:
    first = register_and_login(db_client)
    refresh(db_client, first["refresh_token"])
    refresh(db_client, first["refresh_token"])  # reuse: everything revoked

    fresh = login(db_client)  # very likely within the same second

    assert me(db_client, fresh["access_token"]).status_code == 200
    assert refresh(db_client, fresh["refresh_token"]).status_code == 200


def test_a_revocation_does_not_touch_other_users(db_client: TestClient) -> None:
    mine = register_and_login(db_client)
    theirs = register_and_login(db_client, "other@example.com")
    refresh(db_client, mine["refresh_token"])

    refresh(db_client, mine["refresh_token"])  # reuse on MY account

    assert me(db_client, theirs["access_token"]).status_code == 200


def test_tokens_issued_before_the_moment_are_refused(db_client: TestClient, run_db: RunDb) -> None:
    tokens = register_and_login(db_client)
    future = datetime(2100, 1, 1, tzinfo=UTC)

    run_db(
        lambda session: session.execute(
            update(User).where(User.email == EMAIL).values(tokens_valid_after=future)
        )
    )

    assert me(db_client, tokens["access_token"]).status_code == 401
    assert refresh(db_client, tokens["refresh_token"]).status_code == 401


def test_the_revocation_is_not_an_edit_of_the_account(db_client: TestClient, run_db: RunDb) -> None:
    first = register_and_login(db_client)
    long_ago = datetime(2020, 1, 1, tzinfo=UTC)
    run_db(
        lambda session: session.execute(
            update(User).where(User.email == EMAIL).values(updated_at=long_ago)
        )
    )
    refresh(db_client, first["refresh_token"])

    refresh(db_client, first["refresh_token"])  # reuse

    user = load_user(run_db)
    assert user.tokens_valid_after is not None
    assert user.updated_at == long_ago  # a security event, not an edit of the account


def test_the_check_costs_no_extra_query(
    db_client: TestClient, query_counter: Callable[[], Any]
) -> None:
    tokens = register_and_login(db_client)

    with query_counter() as counter:
        assert me(db_client, tokens["access_token"]).status_code == 200

    assert counter.count == 1, counter.statements  # only the user lookup it always did
