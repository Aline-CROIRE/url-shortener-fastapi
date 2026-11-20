"""Register answers the same for every email: nobody can find out which emails exist.

    new email    -> 202 {"message": ...}, the account is created
    taken email  -> 202 {"message": ...}, nothing changes (same body, same slow hash)

This deliberately replaces brief tests #1 (201 with id, email, tier) and #4 (409 for
a taken email): both answers would tell an attacker that an email is registered.
The account's details come from GET /auth/me/ after logging in.

A chosen USERNAME that is taken still answers 409: usernames are public (every URL
shows its owner's username), and that check runs only for a NEW email.
"""

import logging
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.services.auth as auth_service_module
from app.models import User

AUTH = "/api/v1/auth"
EMAIL = "aline@example.com"
PASSWORD = "a-long-passphrase"

RunDb = Callable[..., Any]


def register(client: TestClient, email: str = EMAIL, password: str = PASSWORD, **extra: Any) -> Any:
    return client.post(f"{AUTH}/register/", json={"email": email, "password": password, **extra})


def login(client: TestClient, email: str = EMAIL, password: str = PASSWORD) -> Any:
    return client.post(f"{AUTH}/login/", data={"username": email, "password": password})


def count_users(run_db: RunDb, email: str = EMAIL) -> int:
    return run_db(lambda session: session.scalar(select(func.count()).where(User.email == email)))


def test_a_new_email_gets_202_with_only_a_message(db_client: TestClient, run_db: RunDb) -> None:
    response = register(db_client)

    assert response.status_code == 202
    assert set(response.json()) == {"message"}  # no id, email or tier to compare
    assert count_users(run_db) == 1
    assert login(db_client).status_code == 200  # the account really exists


@pytest.mark.parametrize("again", [EMAIL, "ALINE@EXAMPLE.COM", "  aline@example.com "])
def test_a_taken_email_gets_exactly_the_same_answer(
    db_client: TestClient, run_db: RunDb, again: str
) -> None:
    first = register(db_client)

    second = register(db_client, again, password="another-password")

    assert second.status_code == first.status_code == 202
    assert second.json() == first.json()
    assert count_users(run_db) == 1


def test_a_taken_email_never_changes_the_existing_account(db_client: TestClient) -> None:
    register(db_client)

    register(db_client, password="attacker-password")

    assert login(db_client).status_code == 200  # the owner's password still works
    assert login(db_client, password="attacker-password").status_code == 401


def test_a_taken_email_takes_the_same_slow_hash(
    db_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    register(db_client)
    hashed: list[str] = []
    real_hash = auth_service_module.hash_password

    def spy(password: str) -> str:
        hashed.append(password)
        return real_hash(password)

    monkeypatch.setattr(auth_service_module, "hash_password", spy)

    register(db_client, password="another-password")

    # Same work as a real registration, so the response time reveals nothing either
    assert hashed == ["another-password"]


def test_a_chosen_username_that_is_taken_is_still_409(db_client: TestClient) -> None:
    register(db_client, username="aline")

    response = register(db_client, "other@example.com", username="aline")

    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"field": "username"}


def test_a_taken_email_with_a_taken_username_reveals_nothing(db_client: TestClient) -> None:
    register(db_client, username="aline")

    # Both are taken: the email is checked first, so the answer is the usual 202
    response = register(db_client, username="aline")

    assert response.status_code == 202


def test_bad_input_is_still_422(db_client: TestClient) -> None:
    assert register(db_client, password="short").status_code == 422
    assert register(db_client, tier="admin").status_code == 422


def test_a_taken_email_is_logged_for_the_admins(
    db_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    register(db_client, "aline.smith@example.com")

    register(db_client, "aline.smith@example.com")

    messages = [r.getMessage() for r in caplog.records if r.name == "app.security"]
    hits = [m for m in messages if "already registered" in m]
    assert hits  # many of these can mean someone is checking which emails exist
    assert all("a***@example.com" in m and "aline.smith" not in m for m in hits)
