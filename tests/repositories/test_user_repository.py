import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.repositories import UserRepository

pytestmark = pytest.mark.anyio


@pytest.fixture
def repo(db_session: AsyncSession) -> UserRepository:
    return UserRepository(db_session)


async def test_add_fills_the_id(repo: UserRepository) -> None:
    user = await repo.add(User(email="aline@example.com", username="aline", hashed_password="h"))

    assert user.id is not None
    assert await repo.get(user.id) is user


async def test_get_unknown_id_returns_none(repo: UserRepository) -> None:
    assert await repo.get(999_999) is None


async def test_get_by_email_ignores_the_case_of_the_input(repo: UserRepository) -> None:
    user = await repo.add(User(email="aline@example.com", username="aline", hashed_password="h"))

    assert await repo.get_by_email("Aline@Example.COM") is user
    assert await repo.get_by_email("nobody@example.com") is None


async def test_get_by_username(repo: UserRepository) -> None:
    user = await repo.add(User(email="aline@example.com", username="aline", hashed_password="h"))

    assert await repo.get_by_username("aline") is user
    assert await repo.get_by_username("someone") is None
