from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address, IPv6Address

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.models import URL, Click, User

pytestmark = pytest.mark.anyio


@pytest.fixture
async def url(db_session: AsyncSession) -> URL:
    owner = User(email="owner@example.com", username="owner", hashed_password="h")
    url = URL(original_url="https://www.python.org/", short_code="aB3dE9x", owner=owner)
    db_session.add(url)  # the owner is saved too (cascade "save-update")
    await db_session.flush()
    return url


async def count(db_session: AsyncSession, model: type) -> int:
    return (await db_session.execute(select(func.count()).select_from(model))).scalar_one()


async def saved(db_session: AsyncSession, click: Click) -> Click:
    db_session.add(click)
    await db_session.flush()
    await db_session.refresh(click)
    return click


# ---------- columns ----------


async def test_clicked_at_is_stamped_by_postgres(db_session: AsyncSession, url: URL) -> None:
    db_now = (await db_session.execute(select(func.now()))).scalar_one()

    click = await saved(db_session, Click(url=url))  # we never set clicked_at

    assert isinstance(click.id, int)
    assert click.clicked_at.tzinfo is not None
    # now() is the transaction start time, so it equals the time read above
    assert abs(click.clicked_at - db_now) < timedelta(seconds=1)


async def test_optional_fields_can_be_empty(db_session: AsyncSession, url: URL) -> None:
    click = await saved(db_session, Click(url=url))

    assert click.ip_address is None
    assert click.user_agent is None
    assert click.referrer is None
    assert click.country is None
    assert click.city is None


async def test_all_fields_are_stored(db_session: AsyncSession, url: URL) -> None:
    click = await saved(
        db_session,
        Click(
            url=url,
            ip_address="41.186.0.10",
            user_agent="Mozilla/5.0 (Linux; Android 14)",
            referrer="https://twitter.com/",
            country="RW",
            city="Kigali",
        ),
    )

    assert click.ip_address == IPv4Address("41.186.0.10")  # INET -> Python ip object
    assert (click.user_agent, click.referrer) == ("Mozilla/5.0 (Linux; Android 14)", "https://twitter.com/")
    assert (click.country, click.city) == ("RW", "Kigali")


async def test_ipv6_is_supported(db_session: AsyncSession, url: URL) -> None:
    click = await saved(db_session, Click(url=url, ip_address="2001:db8::1"))

    assert click.ip_address == IPv6Address("2001:db8::1")


async def test_invalid_ip_is_rejected(db_session: AsyncSession, url: URL) -> None:
    # FastAPI's TestClient reports its address as "testclient": not an IP
    db_session.add(Click(url=url, ip_address="testclient"))
    with pytest.raises(DBAPIError):
        await db_session.flush()


async def test_country_is_a_two_letter_code(db_session: AsyncSession, url: URL) -> None:
    db_session.add(Click(url=url, country="Rwanda"))
    with pytest.raises(DBAPIError, match="value too long"):
        await db_session.flush()


# ---------- foreign key ----------


async def test_url_is_required(db_session: AsyncSession) -> None:
    db_session.add(Click())
    with pytest.raises(IntegrityError, match="url_id"):
        await db_session.flush()


async def test_url_must_exist(db_session: AsyncSession) -> None:
    db_session.add(Click(url_id=999_999))
    with pytest.raises(IntegrityError, match="fk_clicks_url_id_urls"):
        await db_session.flush()


# ---------- relationships and cascade ----------


async def test_relationship_works_on_both_sides(db_session: AsyncSession, url: URL) -> None:
    db_session.add_all([Click(url=url), Click(url=url)])
    await db_session.flush()
    db_session.expunge_all()

    loaded_url = (
        await db_session.execute(select(URL).options(selectinload(URL.clicks)))
    ).scalar_one()
    click = (
        await db_session.execute(select(Click).options(joinedload(Click.url)).limit(1))
    ).scalar_one()

    assert len(loaded_url.clicks) == 2  # url -> clicks
    assert click.url.short_code == "aB3dE9x"  # click -> url


async def test_deleting_a_url_deletes_its_clicks(db_session: AsyncSession, url: URL) -> None:
    db_session.add_all([Click(url=url), Click(url=url)])
    await db_session.flush()

    await db_session.execute(delete(URL).where(URL.id == url.id))

    assert await count(db_session, Click) == 0


async def test_deleting_a_user_deletes_urls_and_clicks(db_session: AsyncSession, url: URL) -> None:
    db_session.add_all([Click(url=url), Click(url=url)])
    await db_session.flush()

    # One DELETE on users: Postgres cascades users -> urls -> clicks
    await db_session.execute(delete(User).where(User.id == url.owner_id))

    assert await count(db_session, URL) == 0
    assert await count(db_session, Click) == 0


# ---------- composite index ----------


async def test_composite_index_has_columns_in_order(db_session: AsyncSession) -> None:
    definition = (
        await db_session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_clicks_url_id_clicked_at'")
        )
    ).scalar_one()

    assert definition.endswith("(url_id, clicked_at)")


async def test_postgres_can_use_the_index_for_one_urls_clicks(
    db_session: AsyncSession, url: URL
) -> None:
    db_session.add_all([Click(url=url) for _ in range(3)])
    await db_session.flush()
    # Tiny tables are faster to scan fully, so ask Postgres to avoid full scans
    await db_session.execute(text("SET LOCAL enable_seqscan = off"))

    since = datetime.now(UTC) - timedelta(days=7)
    plan = (
        await db_session.execute(
            text("EXPLAIN SELECT * FROM clicks WHERE url_id = :id AND clicked_at >= :since"),
            {"id": url.id, "since": since},
        )
    ).scalars()

    assert "ix_clicks_url_id_clicked_at" in "\n".join(plan)
