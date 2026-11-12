import pytest
from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import URL, Tag, User, url_tags

pytestmark = pytest.mark.anyio


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(email="owner@example.com", username="owner", hashed_password="h")
    db_session.add(user)
    await db_session.flush()
    return user


@pytest.fixture
async def tags(db_session: AsyncSession) -> list[Tag]:
    marketing, social = Tag(name="Marketing"), Tag(name="Social")
    db_session.add_all([marketing, social])
    await db_session.flush()
    return [marketing, social]


def make_url(owner: User, short_code: str, tags: list[Tag] | None = None) -> URL:
    return URL(
        original_url="https://www.python.org/",
        short_code=short_code,
        owner=owner,
        tags=tags or [],
    )


async def count(db_session: AsyncSession, table: object) -> int:
    return (await db_session.execute(select(func.count()).select_from(table))).scalar_one()


# ---------- Tag.name ----------


async def test_tag_name_must_be_unique(db_session: AsyncSession, tags: list[Tag]) -> None:
    db_session.add(Tag(name="Marketing"))
    with pytest.raises(IntegrityError, match="uq_tags_name"):
        await db_session.flush()


async def test_tag_name_is_unique_ignoring_case(db_session: AsyncSession, tags: list[Tag]) -> None:
    db_session.add(Tag(name="MARKETING"))
    with pytest.raises(IntegrityError, match="uq_tags_name_lower"):
        await db_session.flush()


async def test_tag_name_is_required(db_session: AsyncSession) -> None:
    db_session.add(Tag())
    with pytest.raises(IntegrityError, match="name"):
        await db_session.flush()


async def test_tag_name_max_length_is_50(db_session: AsyncSession) -> None:
    db_session.add(Tag(name="x" * 51))
    with pytest.raises(DBAPIError, match="value too long"):
        await db_session.flush()


# ---------- many-to-many ----------


async def test_url_has_many_tags_and_tag_has_many_urls(
    db_session: AsyncSession, owner: User, tags: list[Tag]
) -> None:
    marketing, social = tags
    db_session.add_all(
        [
            make_url(owner, "one", tags=[marketing, social]),
            make_url(owner, "two", tags=[marketing]),
        ]
    )
    await db_session.flush()
    db_session.expunge_all()  # force a real reload from Postgres

    url = (
        await db_session.execute(
            select(URL).where(URL.short_code == "one").options(selectinload(URL.tags))
        )
    ).scalar_one()
    tag = (
        await db_session.execute(
            select(Tag).where(Tag.name == "Marketing").options(selectinload(Tag.urls))
        )
    ).scalar_one()

    assert sorted(t.name for t in url.tags) == ["Marketing", "Social"]  # url -> tags
    assert sorted(u.short_code for u in tag.urls) == ["one", "two"]  # tag -> urls
    assert await count(db_session, url_tags) == 3  # one row per (url, tag) pair


async def test_same_pair_cannot_be_linked_twice(
    db_session: AsyncSession, owner: User, tags: list[Tag]
) -> None:
    url = make_url(owner, "one")
    db_session.add(url)
    await db_session.flush()
    link = {"url_id": url.id, "tag_id": tags[0].id}

    await db_session.execute(insert(url_tags).values(**link))
    with pytest.raises(IntegrityError, match="pk_url_tags"):
        await db_session.execute(insert(url_tags).values(**link))


async def test_removing_a_tag_from_a_url_deletes_only_the_link(
    db_session: AsyncSession, owner: User, tags: list[Tag]
) -> None:
    marketing, social = tags
    url = make_url(owner, "one", tags=[marketing, social])
    db_session.add(url)
    await db_session.flush()

    url.tags.remove(social)
    await db_session.flush()

    assert await count(db_session, url_tags) == 1  # only the Marketing link is left
    assert await count(db_session, Tag) == 2  # the Social tag still exists


# ---------- cascade ----------


async def test_deleting_a_url_keeps_its_tags(
    db_session: AsyncSession, owner: User, tags: list[Tag]
) -> None:
    url = make_url(owner, "one", tags=tags)
    db_session.add(url)
    await db_session.flush()

    await db_session.execute(delete(URL).where(URL.id == url.id))

    assert await count(db_session, url_tags) == 0  # links gone
    assert await count(db_session, Tag) == 2  # tags stay


async def test_deleting_a_tag_keeps_its_urls(
    db_session: AsyncSession, owner: User, tags: list[Tag]
) -> None:
    marketing, _ = tags
    db_session.add(make_url(owner, "one", tags=[marketing]))
    await db_session.flush()

    await db_session.execute(delete(Tag).where(Tag.id == marketing.id))

    assert await count(db_session, url_tags) == 0  # link gone
    assert await count(db_session, URL) == 1  # URL stays


async def test_deleting_a_user_removes_links_but_not_tags(
    db_session: AsyncSession, owner: User, tags: list[Tag]
) -> None:
    db_session.add_all([make_url(owner, "one", tags=tags), make_url(owner, "two", tags=tags)])
    await db_session.flush()

    await db_session.execute(delete(User).where(User.id == owner.id))  # users -> urls -> url_tags

    assert await count(db_session, URL) == 0
    assert await count(db_session, url_tags) == 0
    assert await count(db_session, Tag) == 2


# ---------- indexes ----------


async def test_indexes_exist_in_postgres(db_session: AsyncSession) -> None:
    rows = await db_session.execute(
        text("SELECT indexname FROM pg_indexes WHERE tablename IN ('tags', 'url_tags')")
    )
    indexes = set(rows.scalars())

    assert {"uq_tags_name", "uq_tags_name_lower", "pk_url_tags", "ix_url_tags_tag_id"} <= indexes
