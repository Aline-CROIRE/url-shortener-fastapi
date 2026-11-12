from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, MissingGreenlet
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.models import URL, User

pytestmark = pytest.mark.anyio


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(email="owner@example.com", username="owner", hashed_password="h")
    db_session.add(user)
    await db_session.flush()
    return user


def make_url(owner: User, **overrides: object) -> URL:
    data = {"original_url": "https://www.python.org/", "short_code": "aB3dE9x", "owner": owner}
    data.update(overrides)
    return URL(**data)


async def count_urls(db_session: AsyncSession) -> int:
    return (await db_session.execute(select(func.count()).select_from(URL))).scalar_one()


# ---------- defaults and columns ----------


async def test_new_url_gets_defaults(db_session: AsyncSession, owner: User) -> None:
    url = make_url(owner)
    db_session.add(url)
    await db_session.flush()
    await db_session.refresh(url)

    assert url.is_active is True
    assert url.click_count == 0
    assert url.expires_at is None
    assert url.custom_alias is None
    assert url.title is None and url.description is None and url.favicon is None
    assert url.created_at.tzinfo is not None


async def test_expires_at_keeps_the_timezone(db_session: AsyncSession, owner: User) -> None:
    expiry = datetime.now(UTC) + timedelta(days=7)
    url = make_url(owner, expires_at=expiry)
    db_session.add(url)
    await db_session.flush()
    await db_session.refresh(url)

    assert url.expires_at == expiry
    assert url.expires_at.tzinfo is not None


# ---------- constraints ----------


async def test_short_code_must_be_unique(db_session: AsyncSession, owner: User) -> None:
    db_session.add(make_url(owner))
    await db_session.flush()

    db_session.add(make_url(owner))
    with pytest.raises(IntegrityError, match="ix_urls_short_code"):
        await db_session.flush()


async def test_short_code_max_length_is_10(db_session: AsyncSession, owner: User) -> None:
    db_session.add(make_url(owner, short_code="a" * 10))  # exactly 10: fine
    await db_session.flush()

    db_session.add(make_url(owner, short_code="b" * 11))
    with pytest.raises(DBAPIError, match="value too long"):
        await db_session.flush()


async def test_custom_alias_must_equal_short_code(db_session: AsyncSession, owner: User) -> None:
    db_session.add(make_url(owner, short_code="promo", custom_alias="promo"))  # fine
    await db_session.flush()

    db_session.add(make_url(owner, short_code="xYz1234", custom_alias="other"))
    with pytest.raises(IntegrityError, match="ck_urls_custom_alias_matches_short_code"):
        await db_session.flush()


async def test_many_urls_may_have_no_alias(db_session: AsyncSession, owner: User) -> None:
    # NULL is "unknown", so unique allows many NULLs
    db_session.add_all([make_url(owner, short_code="one"), make_url(owner, short_code="two")])
    await db_session.flush()

    assert await count_urls(db_session) == 2


async def test_click_count_cannot_be_negative(db_session: AsyncSession, owner: User) -> None:
    db_session.add(make_url(owner, click_count=-1))
    with pytest.raises(IntegrityError, match="ck_urls_click_count_non_negative"):
        await db_session.flush()


async def test_owner_is_required(db_session: AsyncSession) -> None:
    db_session.add(URL(original_url="https://x.com/", short_code="noowner"))
    with pytest.raises(IntegrityError, match="owner_id"):
        await db_session.flush()


async def test_owner_must_exist(db_session: AsyncSession) -> None:
    db_session.add(URL(original_url="https://x.com/", short_code="ghost", owner_id=999_999))
    with pytest.raises(IntegrityError, match="fk_urls_owner_id_users"):
        await db_session.flush()


# ---------- relationships ----------


async def test_relationship_works_on_both_sides(db_session: AsyncSession, owner: User) -> None:
    db_session.add_all([make_url(owner, short_code="one"), make_url(owner, short_code="two")])
    await db_session.flush()
    db_session.expunge_all()  # forget the objects: force a real reload from Postgres

    user = (
        await db_session.execute(select(User).options(selectinload(User.urls)))
    ).scalar_one()
    url = (
        await db_session.execute(
            select(URL).where(URL.short_code == "one").options(joinedload(URL.owner))
        )
    ).scalar_one()

    assert sorted(u.short_code for u in user.urls) == ["one", "two"]  # user -> urls
    assert url.owner.email == "owner@example.com"  # url -> owner


async def test_lazy_loading_is_not_allowed_in_async(db_session: AsyncSession, owner: User) -> None:
    db_session.add(make_url(owner))
    await db_session.flush()
    db_session.expunge_all()

    url = (await db_session.execute(select(URL))).scalar_one()  # owner NOT loaded

    # Async code cannot run a hidden SELECT: we must load relationships on purpose
    with pytest.raises(MissingGreenlet):
        _ = url.owner


# ---------- cascade ----------


async def test_deleting_a_user_deletes_their_urls_orm(db_session: AsyncSession, owner: User) -> None:
    db_session.add_all([make_url(owner, short_code="one"), make_url(owner, short_code="two")])
    await db_session.flush()

    await db_session.delete(owner)  # ORM delete; passive_deletes lets Postgres cascade
    await db_session.flush()

    assert await count_urls(db_session) == 0


async def test_deleting_a_user_deletes_their_urls_sql(db_session: AsyncSession, owner: User) -> None:
    db_session.add(make_url(owner))
    await db_session.flush()

    # Plain SQL DELETE: no Python involved, so this proves ON DELETE CASCADE in Postgres
    await db_session.execute(delete(User).where(User.id == owner.id))

    assert await count_urls(db_session) == 0


# ---------- indexes ----------


async def test_indexes_exist_in_postgres(db_session: AsyncSession) -> None:
    rows = await db_session.execute(
        text("SELECT indexname FROM pg_indexes WHERE tablename = 'urls'")
    )
    indexes = set(rows.scalars())

    assert {"ix_urls_short_code", "ix_urls_created_at", "ix_urls_owner_id_created_at_id"} <= indexes
