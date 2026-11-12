from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.db.database import get_db
from app.main import create_app

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
    # Values passed here win over .env and environment variables,
    # so tests always use in-memory storage and never need Redis.
    settings = Settings(
        environment="testing",
        storage_backend="memory",
        base_url=TEST_BASE_URL,
        debug=False,
    )
    # The app itself must also point at the test database, never the real one
    settings.database_url = settings.test_database_url
    return settings


@pytest.fixture
def app(test_settings: Settings) -> Iterator[FastAPI]:
    app = create_app(test_settings)
    yield app
    app.dependency_overrides.clear()


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

    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as test_client:
        # Inside TestClient's event loop: every request of this test uses this connection
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
def anyio_backend() -> str:
    # Run @pytest.mark.anyio tests with asyncio (anyio's pytest plugin comes with FastAPI)
    return "asyncio"
