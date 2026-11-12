from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings
from app.db.database import get_db
from app.main import create_app

TEST_BASE_URL = "http://short.test/"  # trailing slash on purpose: it must be removed


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
def db_client(app: FastAPI, test_settings: Settings) -> Iterator[TestClient]:
    """A TestClient whose get_db uses the test database inside a transaction
    that is rolled back after the test, so every test starts with a clean database.
    """
    # NullPool: no connection is kept between tests (each async connection
    # belongs to one event loop, and TestClient starts a new loop per test).
    engine = create_async_engine(test_settings.test_database_url, poolclass=NullPool)
    state: dict[str, AsyncConnection] = {}

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        if "connection" not in state:
            # First request of this test: open the connection and the outer transaction
            connection = await engine.connect()
            await connection.begin()
            state["connection"] = connection
        # "create_savepoint": a commit inside the app only releases a SAVEPOINT,
        # the outer transaction stays open and is rolled back at the end
        async with AsyncSession(
            bind=state["connection"],
            join_transaction_mode="create_savepoint",
            expire_on_commit=False,
        ) as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as test_client:
        yield test_client
        # Still inside TestClient's event loop: undo everything, then disconnect
        if "connection" in state:
            connection = state["connection"]
            test_client.portal.call(connection.rollback)
            test_client.portal.call(connection.close)
        test_client.portal.call(engine.dispose)


@pytest.fixture
def anyio_backend() -> str:
    # Run @pytest.mark.anyio tests with asyncio (anyio's pytest plugin comes with FastAPI)
    return "asyncio"
