"""Alignment with the project specification (rubric review fixes):

1. paths with a trailing slash are served directly (no 307 redirect)
2. URL.objects is a Django-style manager
3. User mirrors AbstractUser; is_premium is a read-only property of tier (Module 7)
4. referrer is stored only when it is a real http(s) URL
"""

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from app.api.visitor import clean_referrer
from app.core.config import get_settings
from app.main import app as real_app
from app.models import URL, Click, User, UserTier
from app.models.url import URLManager
from app.repositories import URLRepository
from tests.conftest import alembic_config

RunDb = Callable[..., Any]
TARGET = "https://www.python.org/"


# ---------- 1. trailing slash paths (the specification's form) ----------


def test_every_specification_path_answers_directly(db_client: TestClient, owner_id: int) -> None:
    created = db_client.post("/api/v1/urls/", json={"original_url": TARGET})
    assert created.status_code == 201
    code = created.json()["short_code"]

    assert db_client.get("/api/v1/urls/").status_code == 200
    assert db_client.get(f"/api/v1/urls/{code}/").status_code == 200
    assert db_client.put(f"/api/v1/urls/{code}/", json={"title": "Python"}).status_code == 200
    assert db_client.get(f"/api/v1/analytics/{code}/").status_code == 200
    redirect = db_client.get(f"/{code}/")
    assert redirect.status_code == 302
    assert redirect.headers["location"] == TARGET
    assert db_client.delete(f"/api/v1/urls/{code}/").status_code == 204


def test_paths_without_the_slash_still_work(db_client: TestClient, owner_id: int) -> None:
    code = db_client.post("/api/v1/urls", json={"original_url": TARGET}).json()["short_code"]

    assert db_client.get(f"/api/v1/urls/{code}").status_code == 200
    assert db_client.get(f"/{code}").status_code == 302


def test_swagger_documents_the_specification_paths() -> None:
    paths = set(real_app.openapi()["paths"])

    assert {
        "/api/v1/urls/",
        "/api/v1/urls/{short_code}/",
        "/api/v1/analytics/{short_code}/",
        "/{short_code}/",
    } <= paths
    assert "/api/v1/urls" not in paths  # the alias works but is not listed twice


# ---------- 2. the manager ----------


def test_url_objects_is_a_manager_not_a_column() -> None:
    assert isinstance(URL.objects, URLManager)
    assert "objects" not in URL.__table__.columns


def test_repository_reuses_the_manager() -> None:
    def sql(statement: Any) -> str:
        return str(statement.compile())

    assert sql(URLRepository.active_urls()) == sql(URL.objects.active_urls())
    assert sql(URLRepository.expired_urls()) == sql(URL.objects.expired_urls())
    assert sql(URLRepository.popular_urls(5)) == sql(URL.objects.popular_urls(5))


@pytest.mark.anyio
async def test_manager_queries_run_and_compose(db_session: AsyncSession) -> None:
    owner = User(email="m@example.com", username="manager", hashed_password="h")
    db_session.add_all(
        [
            URL(original_url=TARGET, short_code="live01", owner=owner, click_count=5),
            URL(original_url=TARGET, short_code="off01", owner=owner, is_active=False),
        ]
    )
    await db_session.flush()

    live = (
        await db_session.scalars(URL.objects.active_urls().where(URL.owner_id == owner.id))
    ).all()
    top = (
        await db_session.scalars(URL.objects.popular_urls(1).where(URL.owner_id == owner.id))
    ).all()

    assert [u.short_code for u in live] == ["live01"]
    assert [u.short_code for u in top] == ["live01"]


def test_popular_urls_limit_is_checked() -> None:
    with pytest.raises(ValueError, match="between 1 and 100"):
        URL.objects.popular_urls(101)


# ---------- 3. User like AbstractUser, is_premium worked out from tier ----------


@pytest.mark.anyio
async def test_user_has_the_abstractuser_fields_with_defaults(db_session: AsyncSession) -> None:
    user = User(email="a@example.com", username="u" * 150, hashed_password="h")  # 150 = max
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)

    assert (user.first_name, user.last_name) == ("", "")
    assert (user.is_active, user.is_staff, user.is_superuser) == (True, False, False)
    assert user.last_login is None
    assert user.date_joined == user.created_at  # the same column, Django's name


@pytest.mark.anyio
async def test_username_longer_than_150_is_refused(db_session: AsyncSession) -> None:
    db_session.add(User(email="b@example.com", username="u" * 151, hashed_password="h"))
    with pytest.raises(DBAPIError, match="value too long"):
        await db_session.flush()


@pytest.mark.parametrize(
    ("tier", "premium"),
    [(UserTier.FREE, False), (UserTier.PREMIUM, True), (UserTier.ADMIN, True)],
)
def test_setting_the_tier_sets_is_premium(tier: UserTier, premium: bool) -> None:
    user = User(email="c@example.com", username="c", hashed_password="h")

    user.tier = tier

    assert user.is_premium is premium


def test_tier_labels() -> None:
    assert [tier.label for tier in UserTier] == ["Free", "Premium", "Admin"]


def test_is_premium_cannot_be_set_directly() -> None:
    user = User(email="d@example.com", username="d", hashed_password="h")

    with pytest.raises(AttributeError):
        user.is_premium = True  # type: ignore[misc]  # read-only on purpose


def test_new_user_without_a_tier_is_not_premium() -> None:
    # tier's default ("free") is only applied on INSERT; until then it is None
    assert User(email="e@example.com", username="e", hashed_password="h").is_premium is False


@pytest.mark.anyio
async def test_is_premium_is_no_longer_a_column(db_session: AsyncSession) -> None:
    result = await db_session.scalars(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = 'users'")
    )
    columns = set(result.all())

    assert "tier" in columns
    assert "is_premium" not in columns


def test_migration_repairs_old_contradicting_rows() -> None:
    """Go back to before the rule, store a contradiction, come back: it is repaired."""
    url = get_settings().test_database_url
    config = alembic_config(url)

    async def run(sql: str, *, read: bool = False) -> Any:
        """Run one statement (committed); read=True returns the single value of a SELECT."""
        engine = create_async_engine(url, poolclass=NullPool)
        async with engine.begin() as connection:
            result = await connection.execute(text(sql))
            value = result.scalar() if read else None  # INSERT/DELETE return no rows
        await engine.dispose()
        return value

    try:
        command.downgrade(config, "82a55000dc8d")  # the CHECK rule does not exist here
        asyncio.run(
            run(
                "INSERT INTO users (email, username, hashed_password, tier, is_premium) "
                "VALUES ('old@x.com', 'old', 'h', 'free', true)"
            )
        )
        # repair, then add the rule (stop there: the next migration drops the column)
        command.upgrade(config, "dfe41766eb04")
        repaired = asyncio.run(
            run("SELECT is_premium FROM users WHERE email = 'old@x.com'", read=True)
        )
        assert repaired is False
    finally:
        command.upgrade(config, "head")
        asyncio.run(run("DELETE FROM users WHERE email = 'old@x.com'"))


# ---------- 4. referrer must be a real URL ----------


@pytest.mark.parametrize(
    ("header", "stored"),
    [
        ("https://twitter.com/home", "https://twitter.com/home"),
        ("http://example.org", "http://example.org"),
        ("", None),
        ("not a url", None),
        ("android-app://com.slack", None),
        ("javascript:alert(1)", None),
        ("https://", None),
        ("http://[not-an-ipv6", None),  # urlsplit itself raises ValueError
        (None, None),
    ],
)
def test_clean_referrer(header: str | None, stored: str | None) -> None:
    assert clean_referrer(header) == stored


def test_redirect_stores_only_valid_referrers(
    db_client: TestClient, owner_id: int, run_db: RunDb
) -> None:
    code = db_client.post("/api/v1/urls/", json={"original_url": TARGET}).json()["short_code"]

    db_client.get(f"/{code}/", headers={"Referer": "android-app://com.slack"})
    db_client.get(f"/{code}/", headers={"Referer": "https://twitter.com/"})

    async def referrers(session: AsyncSession) -> list[str | None]:
        rows = await session.scalars(
            select(Click.referrer).join(URL).where(URL.short_code == code).order_by(Click.id)
        )
        return list(rows)

    assert run_db(referrers) == [None, "https://twitter.com/"]


def test_is_premium_migration_downgrades_and_upgrades_without_losing_data() -> None:
    """Down: the column comes back, filled from tier. Up again: it is gone."""
    url = get_settings().test_database_url
    config = alembic_config(url)

    async def run(sql: str, *, read: bool = False) -> Any:
        engine = create_async_engine(url, poolclass=NullPool)
        async with engine.begin() as connection:
            result = await connection.execute(text(sql))
            value = result.scalar() if read else None
        await engine.dispose()
        return value

    columns_sql = (
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'users' AND column_name = 'is_premium'"
    )
    try:
        asyncio.run(
            run(
                "INSERT INTO users (email, username, hashed_password, tier) "
                "VALUES ('vip@x.com', 'vip', 'h', 'premium')"
            )
        )
        command.downgrade(config, "dfe41766eb04")  # before the column was dropped
        restored = asyncio.run(
            run("SELECT is_premium FROM users WHERE email = 'vip@x.com'", read=True)
        )
        assert restored is True  # worked out again from tier

        command.upgrade(config, "head")
        assert asyncio.run(run(columns_sql, read=True)) == 0
    finally:
        command.upgrade(config, "head")
        asyncio.run(run("DELETE FROM users WHERE email = 'vip@x.com'"))
