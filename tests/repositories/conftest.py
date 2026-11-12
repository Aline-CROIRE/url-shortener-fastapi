"""Small factories shared by the repository tests."""

from itertools import count

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import URL, User

_numbers = count(1)


@pytest.fixture
async def owner(db_session: AsyncSession) -> User:
    user = User(email="owner@example.com", username="owner", hashed_password="h")
    db_session.add(user)
    await db_session.flush()
    return user


@pytest.fixture
def make_url(db_session: AsyncSession, owner: User):
    """Call it to create one URL: await make_url(click_count=5, ...)."""

    async def _make_url(**fields: object) -> URL:
        data = {
            "original_url": "https://www.python.org/",
            "short_code": f"code{next(_numbers)}",
            "owner": owner,
        }
        data.update(fields)
        url = URL(**data)
        db_session.add(url)
        await db_session.flush()
        return url

    return _make_url
