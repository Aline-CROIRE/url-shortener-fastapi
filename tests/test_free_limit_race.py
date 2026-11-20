"""The Free limit under requests at the same moment (brief test #14, real Postgres).

Without a lock, "count, then insert" is a check-then-act race:

    request A:  count = 9 (OK) ................ INSERT  -> 10
    request B:        count = 9 (OK) .......... INSERT  -> 11  (limit broken!)

With SELECT ... FOR UPDATE on the user's row (taken before counting, in the same
transaction as the insert), B must wait until A commits, and then counts 10:

    request A:  LOCK user, count = 9, INSERT, COMMIT (lock released)
    request B:  LOCK user (waits) ........... count = 10 -> 403
"""

import asyncio
import secrets
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.core.exceptions import FreeTierLimitError
from app.models import URL, User, UserTier
from app.repositories import TagRepository, URLRepository, UserRepository
from app.repositories.url import utc_now
from app.schemas.url import URLCreate, URLUpdate
from app.services.urls import URLService
from tests.concurrency import wait_until_blocked

pytestmark = pytest.mark.anyio

FREE_LIMIT = 10
DOMAIN = "free-race.example.com"
TARGET = "https://www.python.org/"


@pytest.fixture
async def engine(test_settings: Settings) -> AsyncIterator[AsyncEngine]:
    """An engine on the test database; removes everything these tests really commit."""
    engine = create_async_engine(test_settings.test_database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        async with engine.begin() as connection:  # deleting the user deletes its URLs
            await connection.execute(delete(User).where(User.email.like(f"%@{DOMAIN}")))
        await engine.dispose()


@pytest.fixture
def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def service(session: AsyncSession) -> URLService:
    """A URLService on its own session, built like the app builds it."""
    return URLService(
        session=session,
        urls=URLRepository(session),
        users=UserRepository(session),
        tags=TagRepository(session),
    )


async def free_user_with(
    sessions: async_sessionmaker[AsyncSession], live: int, switched_off: int = 0
) -> tuple[int, list[str]]:
    """Commit a Free user with some live and some switched-off URLs; return id + off codes."""
    off_codes = [secrets.token_hex(4) for _ in range(switched_off)]
    async with sessions() as session, session.begin():
        user = User(
            email=f"{secrets.token_hex(4)}@{DOMAIN}",
            username=f"race-{secrets.token_hex(4)}",
            hashed_password="!",
            tier=UserTier.FREE,
        )
        session.add(user)
        await session.flush()
        session.add_all(
            URL(original_url=TARGET, short_code=secrets.token_hex(4), owner_id=user.id)
            for _ in range(live)
        )
        session.add_all(
            URL(original_url=TARGET, short_code=code, owner_id=user.id, is_active=False)
            for code in off_codes
        )
    return user.id, off_codes


async def live_count(sessions: async_sessionmaker[AsyncSession], user_id: int) -> int:
    async with sessions() as session:
        statement = select(func.count()).where(URL.owner_id == user_id, URL.is_live(utc_now()))
        return await session.scalar(statement) or 0


async def test_the_second_create_waits_for_the_first_and_then_gets_403(
    engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """Proves the row lock: without it the second request would not wait (and pass)."""
    user_id, _ = await free_user_with(sessions, live=FREE_LIMIT - 1)

    async with sessions() as first, sessions() as second:
        async with first.begin():
            # "Request A" inside its transaction: user row locked, 10th URL inserted
            await first.execute(select(User).where(User.id == user_id).with_for_update())
            first.add(URL(original_url=TARGET, short_code=secrets.token_hex(4), owner_id=user_id))
            await first.flush()

            racing = asyncio.create_task(
                service(second).create(URLCreate(original_url=TARGET), user_id)
            )
            await wait_until_blocked(engine, racing)  # B is stuck on A's lock
        # A committed: the lock is released, B counts 10 live URLs

        with pytest.raises(FreeTierLimitError):
            await asyncio.wait_for(racing, timeout=10)

    assert await live_count(sessions, user_id) == FREE_LIMIT


async def test_many_creates_at_once_never_pass_the_limit(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    user_id, _ = await free_user_with(sessions, live=FREE_LIMIT - 2)

    async def create_one() -> URL:
        async with sessions() as session:
            return await service(session).create(URLCreate(original_url=TARGET), user_id)

    results = await asyncio.gather(*(create_one() for _ in range(6)), return_exceptions=True)

    created = [r for r in results if isinstance(r, URL)]
    refused = [r for r in results if isinstance(r, FreeTierLimitError)]
    assert len(created) == 2  # exactly the 2 free places
    assert len(refused) == 4  # everyone else gets a clean 403, never a 500
    assert await live_count(sessions, user_id) == FREE_LIMIT


async def test_switching_a_url_on_waits_for_a_create_and_then_gets_403(
    engine: AsyncEngine, sessions: async_sessionmaker[AsyncSession]
) -> None:
    """The UPDATE path takes the same user lock: a switch-on cannot slip past the limit."""
    user_id, (off_code,) = await free_user_with(sessions, live=FREE_LIMIT - 1, switched_off=1)

    async with sessions() as first, sessions() as second:
        async with first.begin():
            # "Request A" (a create) holds the user lock and adds the 10th live URL
            await first.execute(select(User).where(User.id == user_id).with_for_update())
            first.add(URL(original_url=TARGET, short_code=secrets.token_hex(4), owner_id=user_id))
            await first.flush()

            racing = asyncio.create_task(
                service(second).update(off_code, user_id, URLUpdate(is_active=True))
            )
            await wait_until_blocked(engine, racing)  # the switch-on waits for A
        # A committed: the switch-on now counts 10 live URLs

        with pytest.raises(FreeTierLimitError):
            await asyncio.wait_for(racing, timeout=10)

    assert await live_count(sessions, user_id) == FREE_LIMIT
