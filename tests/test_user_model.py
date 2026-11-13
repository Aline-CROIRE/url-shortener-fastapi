import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User, UserTier

pytestmark = pytest.mark.anyio  # every test in this file is async


def make_user(**overrides: object) -> User:
    data = {
        "email": "aline@example.com",
        "username": "aline",
        "hashed_password": "not-a-real-hash",
    }
    data.update(overrides)
    return User(**data)


async def test_new_user_gets_defaults_and_timestamps(db_session: AsyncSession) -> None:
    user = make_user()
    db_session.add(user)
    await db_session.flush()  # send the INSERT now (the test rollback undoes it later)
    await db_session.refresh(user)  # read back what Postgres stored

    assert isinstance(user.id, int)
    assert user.is_premium is False
    assert user.tier is UserTier.FREE
    assert user.created_at.tzinfo is not None  # timezone-aware
    assert user.updated_at is not None


async def test_server_defaults_work_without_python(db_session: AsyncSession) -> None:
    # Raw SQL skips Python's default=..., so only server_default can fill these
    await db_session.execute(
        text(
            "INSERT INTO users (email, username, hashed_password) VALUES ('raw@x.com', 'raw', 'h')"
        )
    )
    row = (
        await db_session.execute(text("SELECT is_premium, tier FROM users WHERE username = 'raw'"))
    ).one()

    assert row.is_premium is False
    assert row.tier == "free"


async def test_tier_is_stored_as_its_value(db_session: AsyncSession) -> None:
    db_session.add(make_user(tier=UserTier.PREMIUM, is_premium=True))
    await db_session.flush()

    stored = (await db_session.execute(text("SELECT tier::text FROM users"))).scalar_one()
    loaded = (await db_session.execute(select(User))).scalar_one()

    assert stored == "premium"  # the value, not the Python name "PREMIUM"
    assert loaded.tier is UserTier.PREMIUM


async def test_duplicate_email_is_rejected(db_session: AsyncSession) -> None:
    db_session.add(make_user())
    await db_session.flush()

    db_session.add(make_user(username="someone-else"))
    with pytest.raises(IntegrityError, match="uq_users_email"):
        await db_session.flush()


async def test_duplicate_username_is_rejected(db_session: AsyncSession) -> None:
    db_session.add(make_user())
    await db_session.flush()

    db_session.add(make_user(email="other@example.com"))
    with pytest.raises(IntegrityError, match="uq_users_username"):
        await db_session.flush()


async def test_email_is_required(db_session: AsyncSession) -> None:
    db_session.add(make_user(email=None))
    with pytest.raises(IntegrityError, match="email"):
        await db_session.flush()


async def test_unknown_tier_is_rejected_by_postgres(db_session: AsyncSession) -> None:
    with pytest.raises(DBAPIError, match="user_tier"):
        await db_session.execute(
            text(
                "INSERT INTO users (email, username, hashed_password, tier) "
                "VALUES ('x@x.com', 'x', 'h', 'gold')"
            )
        )
