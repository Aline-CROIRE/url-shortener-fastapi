"""Two registrations at the same moment (Module 6 lesson: draw the timeline).

Each test uses two REAL connections. The first one inserts a user and does NOT
commit yet. The second one registers through the AuthService: its INSERT has to
wait, because Postgres cannot know yet whether the first one will commit. When the
first commits, the second gets a unique violation. That must become a clean
409 (or a different username), never a 500.

    first:  INSERT race@x ......... COMMIT
    second:        hash, INSERT race@x (waits) -> unique violation -> 409
"""

import asyncio
import time
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import app.services.auth as auth_service_module
from app.core.config import Settings
from app.core.exceptions import ConflictError
from app.models import User
from app.repositories import UserRepository
from app.schemas.auth import UserRegister
from app.services.auth import AuthService

pytestmark = pytest.mark.anyio

PASSWORD = "a-long-passphrase"
DOMAINS = ("race-one.example.com", "race-two.example.com")


@pytest.fixture
async def engine(test_settings: Settings) -> AsyncIterator[AsyncEngine]:
    """An engine on the test database; removes the users these tests really commit."""
    engine = create_async_engine(test_settings.test_database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        async with engine.begin() as connection:
            for domain in DOMAINS:
                await connection.execute(delete(User).where(User.email.like(f"%@{domain}")))
        await engine.dispose()


async def wait_until_blocked(engine: AsyncEngine, racing: asyncio.Task[User]) -> None:
    """Return once the racing registration is blocked on a row lock.

    No fixed sleep, so the test is not flaky on a slow machine. If the racing task
    ends before it ever waited, its own result (or real error) is raised at once.
    """
    deadline = time.monotonic() + 10
    # AUTOCOMMIT: Postgres takes ONE snapshot of pg_stat_activity per transaction, so
    # inside a single transaction the watcher would never see the second one start waiting
    async with engine.connect() as connection:
        watcher = await connection.execution_options(isolation_level="AUTOCOMMIT")
        while time.monotonic() < deadline:
            if racing.done():
                await racing  # raises the task's real error, if it had one
                raise AssertionError("the second registration finished without waiting")
            waiting = await watcher.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND datname = current_database()"
                )
            )
            if waiting:
                return
            await asyncio.sleep(0.02)
    racing.cancel()
    raise AssertionError("the second registration never waited for the first")


async def register_while_first_holds(engine: AsyncEngine, held: User, data: UserRegister) -> User:
    """The first session inserts `held` (uncommitted); the second registers `data`."""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as first, sessions() as second:
        first.add(held)
        await first.flush()  # the row exists, but only for the first session

        service = AuthService(session=second, users=UserRepository(second))
        racing = asyncio.create_task(service.register(data))
        await wait_until_blocked(engine, racing)

        await first.commit()
        return await asyncio.wait_for(racing, timeout=10)


async def test_the_same_email_at_the_same_moment_is_409_not_500(engine: AsyncEngine) -> None:
    email = f"race@{DOMAINS[0]}"
    held = User(email=email, username="race-first", hashed_password="h")

    with pytest.raises(ConflictError) as error:
        await register_while_first_holds(engine, held, UserRegister(email=email, password=PASSWORD))

    assert error.value.details == {"field": "email"}


async def test_the_same_made_username_at_the_same_moment_still_registers(
    engine: AsyncEngine,
) -> None:
    held = User(email=f"racer@{DOMAINS[0]}", username="racer", hashed_password="h")

    # The second person never asked for "racer": they must get an account, not a 409
    user = await register_while_first_holds(
        engine, held, UserRegister(email=f"racer@{DOMAINS[1]}", password=PASSWORD)
    )

    assert user.username != "racer"
    assert user.username.startswith("racer")
    async with AsyncSession(engine) as session:
        stored = await session.scalar(select(User.username).where(User.id == user.id))
    assert stored == user.username  # really committed


async def test_a_chosen_username_taken_at_the_same_moment_is_409(engine: AsyncEngine) -> None:
    held = User(email=f"first@{DOMAINS[0]}", username="race-chosen", hashed_password="h")
    data = UserRegister(email=f"second@{DOMAINS[1]}", password=PASSWORD, username="race-chosen")

    with pytest.raises(ConflictError) as error:
        await register_while_first_holds(engine, held, data)

    assert error.value.details == {"field": "username"}


async def test_it_gives_up_with_409_when_no_free_username_is_found(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Make every "random" number the same, and take both possible names
    monkeypatch.setattr(auth_service_module, "username_suffix", lambda: "7")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add_all(
            [
                User(email=f"a@{DOMAINS[0]}", username="unlucky", hashed_password="h"),
                User(email=f"b@{DOMAINS[0]}", username="unlucky7", hashed_password="h"),
            ]
        )
        await session.commit()

    async with sessions() as session:
        service = AuthService(session=session, users=UserRepository(session))
        with pytest.raises(ConflictError) as error:
            await service.register(UserRegister(email=f"unlucky@{DOMAINS[1]}", password=PASSWORD))

    # A clear way out instead of looping forever or failing with a 500
    assert error.value.details == {"field": "username"}
    assert "choose" in error.value.message.lower()
