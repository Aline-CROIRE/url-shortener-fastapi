from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

TEST_BASE_URL = "http://short.test/"  # trailing slash on purpose: it must be removed


@pytest.fixture
def test_settings() -> Settings:
    # Values passed here win over .env and environment variables,
    # so tests always use in-memory storage and never need Redis.
    return Settings(
        environment="testing",
        storage_backend="memory",
        base_url=TEST_BASE_URL,
        debug=False,
    )


@pytest.fixture
def app(test_settings: Settings) -> FastAPI:
    return create_app(test_settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    # "with" runs the lifespan (startup/shutdown), which creates the repository.
    # follow_redirects=False lets us check the 302 itself.
    # raise_server_exceptions=False lets us see the JSON 500 response.
    with TestClient(app, follow_redirects=False, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def anyio_backend() -> str:
    # Run @pytest.mark.anyio tests with asyncio (anyio's pytest plugin comes with FastAPI)
    return "asyncio"
