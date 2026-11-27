import itertools
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# JWT_SECRET_KEY is required (the app refuses to start without it). Set a test-only
# value BEFORE importing the app: importing app.main already builds the settings.
# A value already set in the environment still wins.
TEST_JWT_SECRET_KEY = "test-only-jwt-secret-key-at-least-32-chars"
os.environ["JWT_SECRET_KEY"] = os.environ.get("JWT_SECRET_KEY", TEST_JWT_SECRET_KEY)
# Rate-limit counters in MEMORY for the test suite, even if .env sets REDIS_URL: the
# tests reset the counters all the time and must never touch a real Redis. The Redis
# storage itself is tested on its own database in tests/test_rate_limit_storage.py.
os.environ["RATE_LIMIT_STORAGE_URI"] = "memory://"

import pytest
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from alembic import command
from app.api.auth import get_current_user, get_session_factory
from app.api.dependencies import get_click_tracker
from app.core.config import Settings, get_settings, to_sync_url
from app.core.rate_limit import limiter
from app.core.security import create_access_token
from app.db.database import get_db, get_read_db
from app.db.query_counter import QueryCounter, count_queries
from app.main import create_app
from app.models import User, UserTier
from app.services.clicks import ClickEvent, click_statements
from app.worker import tasks
from app.worker.celery_app import celery_app

TEST_BASE_URL = "http://short.test/"  # trailing slash on purpose: it must be removed
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def alembic_config(database_url: str) -> Config:
    """Alembic settings from alembic.ini, pointed at the given database."""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture(scope="session", autouse=True)
def migrated_test_database() -> None:
    """Once per test run: bring the TEST database to the newest migration.

    The schema comes from Alembic, exactly like production (no create_all()).
    """
    command.upgrade(alembic_config(get_settings().test_database_url), "head")


@pytest.fixture
def test_settings() -> Settings:
    # Values passed here win over .env and environment variables
    settings = Settings(
        environment="testing",
        base_url=TEST_BASE_URL,
        redis_url="",  # no real Redis in tests (cache tests use a fake one)
        debug=False,
    )
    # The app itself must also point at the test database, never the real one
    settings.database_url = settings.test_database_url
    return settings


class RecordingClickTracker:
    """Tests only: remembers each click handed over by the redirect, writes nothing."""

    def __init__(self) -> None:
        self.events: list[ClickEvent] = []

    async def track(self, event: ClickEvent) -> None:
        self.events.append(event)


class InlineClickTracker:
    """Tests only: saves the click at once, with the SAME SQL statements as the Celery
    task, inside the test transaction (a worker's own connection could not see it).
    """

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory = session_factory

    async def track(self, event: ClickEvent) -> None:
        async with self._session_factory() as session:
            for statement in click_statements(event):
                await session.execute(statement)
            await session.commit()


@pytest.fixture
def app(test_settings: Settings) -> Iterator[FastAPI]:
    app = create_app(test_settings)
    # Never send a test click to a real broker (a running worker would pick it up)
    app.dependency_overrides[get_click_tracker] = RecordingClickTracker
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def recorded_clicks(app: FastAPI) -> list[ClickEvent]:
    """The clicks the redirect hands over, recorded instead of saved.

    List it AFTER db_client in a test's arguments (the last override wins).
    """
    tracker = RecordingClickTracker()
    app.dependency_overrides[get_click_tracker] = lambda: tracker
    return tracker.events


@pytest.fixture
def worker_db(test_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[Session]:
    """A SYNC session on the test database for the Celery task (psycopg, like the worker).

    The task's own sessions join this connection's transaction (savepoints), and
    everything is rolled back after the test. Use the yielded session to seed and check.
    """
    engine = create_engine(to_sync_url(test_settings.test_database_url), poolclass=NullPool)
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    monkeypatch.setattr(tasks, "get_session_factory", lambda: factory)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
        engine.dispose()


@pytest.fixture
def celery_eager() -> Iterator[None]:
    """Run tasks at once, in the calling thread, instead of sending them to a broker."""
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True  # a failing task raises in the test
    yield
    celery_app.conf.task_always_eager = False
    celery_app.conf.task_eager_propagates = False


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # "with" runs the lifespan (startup/shutdown), which creates the repository.
    # follow_redirects=False lets us check the 302 itself.
    # raise_server_exceptions=False lets us see the JSON 500 response.
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
def db_state() -> dict[str, Any]:
    """Shared between db_client and run_db: the test's connection and client."""
    return {}


@pytest.fixture
def db_client(
    app: FastAPI, test_settings: Settings, db_state: dict[str, Any]
) -> Iterator[TestClient]:
    """A TestClient whose get_db uses the test database inside a transaction
    that is rolled back after the test, so every test starts with a clean database.
    """
    # NullPool: no connection is kept between tests (each async connection
    # belongs to one event loop, and TestClient starts a new loop per test).
    engine = create_async_engine(test_settings.test_database_url, poolclass=NullPool)

    def test_session() -> AsyncSession:
        # "create_savepoint": a commit (or session.begin() block) inside the app only
        # releases a SAVEPOINT; the outer transaction stays open and is rolled back
        return AsyncSession(
            bind=db_state["connection"],
            join_transaction_mode="create_savepoint",
            expire_on_commit=False,
        )

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with test_session() as session:
            yield session

    async def open_connection() -> AsyncConnection:
        connection = await engine.connect()
        await connection.begin()  # the outer transaction, rolled back after the test
        return connection

    app.dependency_overrides[get_db] = override_get_db
    # Reads use the SAME test transaction, so they see the data the test wrote
    # (in production the read side may be a replica, a moment behind)
    app.dependency_overrides[get_read_db] = override_get_db
    # The token check (whose token is this?) opens its own short session: same test transaction
    app.dependency_overrides[get_session_factory] = lambda: test_session
    # Clicks are saved by a Celery worker in production; here, at once in the test transaction
    app.dependency_overrides[get_click_tracker] = lambda: InlineClickTracker(test_session)

    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as test_client:
        # Inside TestClient's event loop: every request of this test uses this connection
        db_state["engine"] = engine  # for counting queries (F3.1)
        db_state["connection"] = test_client.portal.call(open_connection)
        db_state["client"] = test_client
        db_state["session_factory"] = test_session
        yield test_client
        # Undo everything the test wrote, then disconnect
        connection = db_state["connection"]
        test_client.portal.call(connection.rollback)
        test_client.portal.call(connection.close)
        test_client.portal.call(engine.dispose)


@pytest.fixture
def run_db(db_client: TestClient, db_state: dict[str, Any]) -> Callable[..., Any]:
    """Run an async function with a session on the SAME connection the app uses.

    Example:  url = run_db(lambda session: create_url(session, ...))
    Use it to put data in the database before a request, or to check data after.
    Its commit only releases a savepoint: the test rollback still undoes it.
    """

    def run(work: Callable[[AsyncSession], Awaitable[Any]]) -> Any:
        async def runner() -> Any:
            async with db_state["session_factory"]() as session:
                result = await work(session)
                await session.commit()
                return result

        return db_state["client"].portal.call(runner)

    return run


@pytest.fixture
async def db_session(test_settings: Settings) -> AsyncIterator[AsyncSession]:
    """A session on the test database for testing models directly (no HTTP).

    Everything happens inside one transaction that is rolled back at the end.
    Use it in tests marked with @pytest.mark.anyio.
    """
    engine = create_async_engine(test_settings.test_database_url, poolclass=NullPool)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            join_transaction_mode="create_savepoint",
            expire_on_commit=False,
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
    await engine.dispose()


@pytest.fixture
def query_counter(
    db_client: TestClient, db_state: dict[str, Any]
) -> Callable[[], AbstractContextManager[QueryCounter]]:
    """Count the SQL statements sent while a block runs:

    with query_counter() as counter:
        db_client.get("/api/v1/urls")
    assert counter.count <= 3, counter.statements
    """
    return lambda: count_queries(db_state["engine"])


@pytest.fixture
def owner_id(app: FastAPI, run_db: Callable[..., Any]) -> int:
    """Seed a user and make the app treat them as the current user (no token needed).

    A shortcut for the Module 5-6 tests, which are not about login or tiers. The
    user is PREMIUM, so those tests are not limited by the Free rules (10 URLs, no
    alias). Login is tested in tests/test_auth.py, tiers in tests/test_tiers.py.
    """

    async def create_owner(session: AsyncSession) -> User:
        user = User(
            email="me@example.com", username="me", hashed_password="!", tier=UserTier.PREMIUM
        )
        session.add(user)
        await session.flush()
        return user

    user = run_db(create_owner)
    app.dependency_overrides[get_current_user] = lambda: user
    return user.id


@dataclass(frozen=True)
class AuthedUser:
    """A user created for a test, with the header that logs them in."""

    id: int
    email: str
    headers: dict[str, str]


@pytest.fixture
def make_user(run_db: Callable[..., Any], test_settings: Settings) -> Callable[..., AuthedUser]:
    """Create a user of any tier directly in the database and give them a real token.

    Promoting users by writing to the database is what the brief asks for: there
    is no public endpoint for changing tiers. The token is a real access token,
    so every request goes through the real Bearer check.

        free = make_user()
        admin = make_user(UserTier.ADMIN)
        client.get("/api/v1/urls/", headers=admin.headers)
    """
    numbers = itertools.count(1)

    def make(tier: UserTier = UserTier.FREE, *, is_active: bool = True) -> AuthedUser:
        name = f"{tier.value}{next(numbers)}"
        email = f"{name}@example.com"

        async def create(session: AsyncSession) -> int:
            user = User(
                email=email, username=name, hashed_password="!", tier=tier, is_active=is_active
            )
            session.add(user)
            await session.flush()
            return user.id

        user_id = run_db(create)
        token = create_access_token(user_id, test_settings)
        return AuthedUser(id=user_id, email=email, headers={"Authorization": f"Bearer {token}"})

    return make


@pytest.fixture(autouse=True)
def reset_rate_limits() -> Iterator[None]:
    """Start every test with empty login rate-limit counters (they live in memory)."""
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def anyio_backend() -> str:
    # Run @pytest.mark.anyio tests with asyncio (anyio's pytest plugin comes with FastAPI)
    return "asyncio"
