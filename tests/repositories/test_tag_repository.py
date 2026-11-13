import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Tag
from app.repositories import TagRepository

pytestmark = pytest.mark.anyio

# Seeded by the data migration (F1.7), so they exist in the test database
SEEDED = {"Marketing", "Social", "Email", "Ads", "Events", "Personal"}


@pytest.fixture
def repo(db_session: AsyncSession) -> TagRepository:
    return TagRepository(db_session)


async def tag_count(db_session: AsyncSession) -> int:
    return (await db_session.execute(select(func.count()).select_from(Tag))).scalar_one()


async def test_get_by_name_ignores_case(repo: TagRepository) -> None:
    tag = await repo.get_by_name("mArKeTiNg")

    assert tag is not None and tag.name == "Marketing"
    assert await repo.get_by_name("does-not-exist") is None


async def test_all_tags_is_sorted_by_name(repo: TagRepository) -> None:
    names = [tag.name for tag in await repo.fetch_all(repo.all_tags())]

    assert set(names) >= SEEDED
    assert names == sorted(names)


async def test_get_or_create_many_creates_only_missing_tags(
    repo: TagRepository, db_session: AsyncSession
) -> None:
    before = await tag_count(db_session)

    tags = await repo.get_or_create_many(["ads", "Launch", "Promo"])

    assert [t.name for t in tags] == ["Ads", "Launch", "Promo"]  # existing spelling kept
    assert all(t.id is not None for t in tags)
    assert await tag_count(db_session) == before + 2  # only Launch and Promo are new


async def test_get_or_create_many_removes_duplicates(
    repo: TagRepository, db_session: AsyncSession
) -> None:
    before = await tag_count(db_session)

    tags = await repo.get_or_create_many(["Launch", "LAUNCH", "launch"])

    assert [t.name for t in tags] == ["Launch"]  # the first spelling wins
    assert await tag_count(db_session) == before + 1


async def test_get_or_create_many_twice_creates_nothing_new(
    repo: TagRepository, db_session: AsyncSession
) -> None:
    first = await repo.get_or_create_many(["Launch"])
    before = await tag_count(db_session)

    second = await repo.get_or_create_many(["launch"])

    assert second[0].id == first[0].id
    assert await tag_count(db_session) == before


async def test_get_or_create_many_with_no_names(repo: TagRepository) -> None:
    assert await repo.get_or_create_many([]) == []
