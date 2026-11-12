"""Deleting a user removes EVERYTHING they own, in one step, through the whole chain:

    users -> urls (also soft-deleted ones) -> clicks
                                           -> url_tags links
Tags are shared, so they stay. Other users' data is untouched.
"""

from typing import Any

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import URL, Click, Tag, User, url_tags

pytestmark = pytest.mark.anyio


async def count(session: AsyncSession, table: Any, *where: Any) -> int:
    return await session.scalar(select(func.count()).select_from(table).where(*where))


@pytest.fixture
async def world(db_session: AsyncSession) -> dict[str, Any]:
    """Alice (to delete) and Bob (must survive) share the tag "Shared"."""
    shared, only_alice = Tag(name="Shared"), Tag(name="AliceOnly")
    alice = User(email="alice@example.com", username="alice", hashed_password="h")
    bob = User(email="bob@example.com", username="bob", hashed_password="h")
    active = URL(original_url="https://a.example/", short_code="alice1", owner=alice, tags=[shared, only_alice])
    deleted = URL(  # soft-deleted, but still a row: it must go too
        original_url="https://a.example/old",
        short_code="alice2",
        owner=alice,
        tags=[shared],
        deleted_at=func.now(),
    )
    bobs = URL(original_url="https://b.example/", short_code="bob1", owner=bob, tags=[shared])
    db_session.add_all([active, deleted, bobs])
    await db_session.flush()
    db_session.add_all(
        [Click(url_id=active.id) for _ in range(3)]
        + [Click(url_id=deleted.id) for _ in range(2)]
        + [Click(url_id=bobs.id)]
    )
    await db_session.flush()
    return {
        "alice": alice,
        "alice_url_ids": [active.id, deleted.id],
        "bob_id": bob.id,
        "bob_url_id": bobs.id,
        "tag_ids": [shared.id, only_alice.id],
    }


async def assert_only_alice_is_gone(session: AsyncSession, world: dict[str, Any]) -> None:
    alice_urls = world["alice_url_ids"]

    # Alice and everything below her: gone
    assert await count(session, User, User.email == "alice@example.com") == 0
    assert await count(session, URL, URL.id.in_(alice_urls)) == 0
    assert await count(session, Click, Click.url_id.in_(alice_urls)) == 0
    assert await count(session, url_tags, url_tags.c.url_id.in_(alice_urls)) == 0

    # Shared things and other users: untouched
    assert await count(session, Tag, Tag.id.in_(world["tag_ids"])) == 2
    assert await count(session, User, User.id == world["bob_id"]) == 1
    assert await count(session, URL, URL.id == world["bob_url_id"]) == 1
    assert await count(session, Click, Click.url_id == world["bob_url_id"]) == 1
    assert await count(session, url_tags, url_tags.c.url_id == world["bob_url_id"]) == 1


async def test_before_deleting_everything_exists(db_session: AsyncSession, world: dict[str, Any]) -> None:
    alice_urls = world["alice_url_ids"]
    assert await count(db_session, Click, Click.url_id.in_(alice_urls)) == 5
    assert await count(db_session, url_tags, url_tags.c.url_id.in_(alice_urls)) == 3


async def test_orm_delete_cascades_through_the_whole_chain(
    db_session: AsyncSession, world: dict[str, Any]
) -> None:
    await db_session.delete(world["alice"])  # ORM delete; passive_deletes -> Postgres cascades
    await db_session.flush()

    await assert_only_alice_is_gone(db_session, world)


async def test_sql_delete_cascades_through_the_whole_chain(
    db_session: AsyncSession, world: dict[str, Any]
) -> None:
    # One plain DELETE, no Python involved: proves ON DELETE CASCADE in the database
    await db_session.execute(delete(User).where(User.email == "alice@example.com"))

    await assert_only_alice_is_gone(db_session, world)
