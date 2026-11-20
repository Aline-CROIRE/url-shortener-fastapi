"""Refresh tokens work ONCE (rotation), and a reused one revokes them all (reuse detection).

Why: a refresh token lives 7 days. If it is stolen, rotation alone is not enough:
the thief and the real user would both keep refreshing. With reuse detection,
the moment an already-used refresh token comes back, one of the two is a thief,
so EVERY refresh token of that user is revoked and both must log in again.

    login       -> R1 (stored, unused)
    refresh R1  -> R2 (R1 marked used)        200
    refresh R1  -> reuse! all refresh tokens of the user revoked  401
    refresh R2  -> revoked                                         401

A reuse also sets users.tokens_valid_after, so the access tokens die at once too
(see test_token_revocation.py). Only the jti (the token's id) is stored, never the token.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.exceptions import InvalidTokenError
from app.core.security import create_refresh_token
from app.models import RefreshToken, User
from app.repositories import RefreshTokenRepository, UserRepository
from app.schemas.auth import TokenPair
from app.services.auth import AuthService

AUTH = "/api/v1/auth"
EMAIL = "aline@example.com"
PASSWORD = "a-long-passphrase"

RunDb = Callable[..., Any]


def register_and_login(client: TestClient, email: str = EMAIL) -> dict[str, str]:
    assert (
        client.post(f"{AUTH}/register/", json={"email": email, "password": PASSWORD}).status_code
        == 201
    )
    return login(client, email)


def login(client: TestClient, email: str = EMAIL) -> dict[str, str]:
    response = client.post(f"{AUTH}/login/", data={"username": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


def refresh(client: TestClient, token: str) -> Any:
    return client.post(f"{AUTH}/refresh/", json={"refresh_token": token})


def stored_rows(run_db: RunDb, email: str = EMAIL) -> list[RefreshToken]:
    async def load(session: AsyncSession) -> list[RefreshToken]:
        rows = await session.scalars(
            select(RefreshToken)
            .join(User)
            .where(User.email == email)
            .order_by(RefreshToken.created_at)
        )
        return list(rows)

    return run_db(load)


# ---------- rotation: every refresh token works once ----------


def test_a_refresh_token_works_only_once(db_client: TestClient) -> None:
    first = register_and_login(db_client)

    assert refresh(db_client, first["refresh_token"]).status_code == 200
    again = refresh(db_client, first["refresh_token"])

    assert again.status_code == 401
    assert again.json()["error"]["code"] == "invalid_token"


def test_the_new_refresh_token_keeps_working(db_client: TestClient) -> None:
    token = register_and_login(db_client)["refresh_token"]

    for _ in range(3):  # a normal client refreshes again and again
        response = refresh(db_client, token)
        assert response.status_code == 200
        token = response.json()["refresh_token"]


# ---------- reuse detection: a used token coming back revokes everything ----------


def test_reusing_a_used_token_revokes_every_refresh_token_of_the_user(
    db_client: TestClient,
) -> None:
    phone = register_and_login(db_client)
    laptop = login(db_client)  # a second device, its own refresh token
    stolen = phone["refresh_token"]
    newest = refresh(db_client, stolen).json()["refresh_token"]  # the real user refreshes

    assert refresh(db_client, stolen).status_code == 401  # the thief replays the old one

    # Nobody can tell thief from user: every refresh token of the account is revoked
    assert refresh(db_client, newest).status_code == 401
    assert refresh(db_client, laptop["refresh_token"]).status_code == 401


def test_reuse_does_not_touch_other_users(db_client: TestClient) -> None:
    mine = register_and_login(db_client)
    theirs = register_and_login(db_client, "other@example.com")
    refresh(db_client, mine["refresh_token"])

    refresh(db_client, mine["refresh_token"])  # reuse on MY account

    assert refresh(db_client, theirs["refresh_token"]).status_code == 200


def test_after_a_revocation_logging_in_again_works(db_client: TestClient) -> None:
    first = register_and_login(db_client)
    refresh(db_client, first["refresh_token"])
    refresh(db_client, first["refresh_token"])  # reuse: all revoked

    again = login(db_client)

    assert refresh(db_client, again["refresh_token"]).status_code == 200


def test_a_reuse_also_kills_the_access_tokens_at_once(db_client: TestClient) -> None:
    first = register_and_login(db_client)
    refresh(db_client, first["refresh_token"])
    refresh(db_client, first["refresh_token"])  # reuse: everything revoked

    # Not "up to 15 more minutes": tokens_valid_after stops it now (test_token_revocation)
    me = db_client.get(f"{AUTH}/me/", headers={"Authorization": f"Bearer {first['access_token']}"})
    assert me.status_code == 401


def test_a_signed_refresh_token_the_server_never_issued_is_refused(
    db_client: TestClient, run_db: RunDb, test_settings: Settings
) -> None:
    register_and_login(db_client)
    user_id = run_db(lambda session: session.scalar(select(User.id).where(User.email == EMAIL)))

    # Correct key and claims, but not in the table (e.g. issued before this change)
    unknown = create_refresh_token(user_id, test_settings)

    assert refresh(db_client, unknown).status_code == 401


# ---------- what is stored ----------


def test_only_the_token_id_is_stored_never_the_token(db_client: TestClient, run_db: RunDb) -> None:
    tokens = register_and_login(db_client)

    (row,) = stored_rows(run_db)

    assert row.used_at is None
    assert row.expires_at > datetime.now(UTC) + timedelta(days=6)
    columns = [str(getattr(row, c.key)) for c in RefreshToken.__table__.columns]
    assert all(tokens["refresh_token"] not in value for value in columns)


def test_old_expired_rows_are_removed_at_the_next_login(
    db_client: TestClient, run_db: RunDb
) -> None:
    register_and_login(db_client)
    user_id = run_db(lambda session: session.scalar(select(User.id).where(User.email == EMAIL)))

    async def add_expired(session: AsyncSession) -> None:
        session.add(
            RefreshToken(
                jti="expired-one", user_id=user_id, expires_at=datetime(2020, 1, 1, tzinfo=UTC)
            )
        )

    run_db(add_expired)
    login(db_client)

    assert "expired-one" not in [row.jti for row in stored_rows(run_db)]


def test_deleting_a_user_deletes_their_refresh_tokens(db_client: TestClient, run_db: RunDb) -> None:
    register_and_login(db_client)

    run_db(lambda session: session.execute(delete(User).where(User.email == EMAIL)))

    count = run_db(lambda session: session.scalar(select(func.count()).select_from(RefreshToken)))
    assert count == 0


# ---------- two refreshes with the same token at the same moment ----------


@pytest.fixture
async def engine(test_settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(test_settings.test_database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        async with engine.begin() as connection:
            await connection.execute(delete(User).where(User.email == "race@rotation.example.com"))
        await engine.dispose()


@pytest.mark.anyio
async def test_two_refreshes_of_one_token_at_once_never_both_succeed(
    engine: AsyncEngine, test_settings: Settings
) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    def service(session: AsyncSession) -> AuthService:
        return AuthService(
            session=session,
            users=UserRepository(session),
            settings=test_settings,
            refresh_tokens=RefreshTokenRepository(session),
        )

    async with sessions() as session, session.begin():
        user = User(
            email="race@rotation.example.com", username="race-rotation", hashed_password="!"
        )
        session.add(user)
    async with sessions() as session:
        pair = await service(session).issue_tokens(user.id)

    async def use_once() -> TokenPair | Exception:
        async with sessions() as session:
            try:
                return await service(session).refresh(pair.refresh_token)
            except InvalidTokenError as exc:
                return exc

    results = await asyncio.gather(use_once(), use_once())

    # "Mark as used" is ONE atomic UPDATE ... WHERE used_at IS NULL: only one wins
    assert sum(isinstance(r, TokenPair) for r in results) == 1
    assert sum(isinstance(r, InvalidTokenError) for r in results) == 1
