from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Click
from app.repositories import ClickRepository

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


@pytest.fixture
def repo(db_session: AsyncSession) -> ClickRepository:
    return ClickRepository(db_session)


async def test_add_fills_id_and_clicked_at(repo: ClickRepository, make_url) -> None:
    url = await make_url()

    click = await repo.add(Click(url=url, user_agent="pytest"))

    assert click.id is not None
    assert click.url_id == url.id


async def test_for_url_returns_only_that_urls_clicks_newest_first(
    repo: ClickRepository, make_url
) -> None:
    url, other = await make_url(), await make_url()
    old = await repo.add(Click(url=url, clicked_at=NOW - timedelta(days=2)))
    new = await repo.add(Click(url=url, clicked_at=NOW))
    middle = await repo.add(Click(url=url, clicked_at=NOW - timedelta(days=1)))
    await repo.add(Click(url=other, clicked_at=NOW))  # someone else's click

    result = await repo.fetch_all(repo.for_url(url.id))

    assert [c.id for c in result] == [new.id, middle.id, old.id]
