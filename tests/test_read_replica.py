"""Read/write routing: GET endpoints use the read-only session, everything else the
primary. No real replica is needed here (see test_replication_integration.py).
"""

from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import ReadDbSession
from app.core.config import Settings
from app.db.database import get_db, get_read_db
from app.main import create_app

API = "/api/v1/urls"
UNREACHABLE_REPLICA = "postgresql+asyncpg://postgres:postgres@127.0.0.1:1/url_shortener"


@pytest.fixture
def routing_log(app: FastAPI, db_client: TestClient, db_state: dict[str, Any]) -> list[str]:
    """Record which session ("write" or "read") each request asked for."""
    log: list[str] = []

    def recording(kind: str) -> Callable[[], AsyncIterator[AsyncSession]]:
        async def dependency() -> AsyncIterator[AsyncSession]:
            log.append(kind)
            async with db_state["session_factory"]() as session:  # the test transaction
                yield session

        return dependency

    app.dependency_overrides[get_db] = recording("write")
    app.dependency_overrides[get_read_db] = recording("read")
    return log


def sessions_used(log: list[str], request: Callable[[], Any]) -> set[str]:
    log.clear()
    response = request()
    assert response.status_code < 400, response.text
    return set(log)


# ---------- which endpoint goes where ----------


def test_endpoints_are_routed_to_the_right_database(
    db_client: TestClient, owner_id: int, routing_log: list[str]
) -> None:
    created = db_client.post(API, json={"original_url": "https://www.python.org/"})
    code = created.json()["short_code"]
    assert set(routing_log) == {"write"}  # create -> primary

    # Reads -> the read-only session (the replica when configured)
    assert sessions_used(routing_log, lambda: db_client.get(API)) == {"read"}
    assert sessions_used(routing_log, lambda: db_client.get(f"{API}/{code}")) == {"read"}
    assert sessions_used(routing_log, lambda: db_client.get(f"/api/v1/analytics/{code}")) == {
        "read"
    }

    # Writes -> the primary. The redirect too: it records a click and must see
    # a link created a moment ago (no replication lag)
    assert sessions_used(routing_log, lambda: db_client.get(f"/{code}")) == {"write"}
    assert sessions_used(
        routing_log, lambda: db_client.put(f"{API}/{code}", json={"title": "x"})
    ) == {"write"}
    assert sessions_used(routing_log, lambda: db_client.delete(f"{API}/{code}")) == {"write"}


# ---------- the read side is read-only ----------


def test_the_read_session_refuses_writes(client: TestClient) -> None:
    # The REAL read session (no override): without a replica it uses the primary,
    # but still read-only, so routing mistakes show up during development
    @client.app.post("/_test/write-on-read")
    async def write_on_read(db: ReadDbSession) -> dict[str, str]:
        try:
            await db.execute(text("INSERT INTO tags (name) VALUES ('should-fail')"))
        except DBAPIError as exc:
            return {"error": str(exc.orig)}
        return {"error": ""}

    error = client.post("/_test/write-on-read").json()["error"]

    assert "cannot execute INSERT in a read-only transaction" in error


def test_without_a_replica_reads_share_the_primary_pool(client: TestClient) -> None:
    state = client.app.state

    assert (
        state.read_engine.sync_engine.pool is state.engine.sync_engine.pool
    )  # no extra connections
    assert state.read_engine.get_execution_options()["postgresql_readonly"] is True


@pytest.fixture
def replica_client(test_settings: Settings) -> Callable[[str], Iterator[TestClient]]:
    def make(replica_url: str) -> TestClient:
        settings = test_settings.model_copy(update={"database_replica_url": replica_url})
        return TestClient(create_app(settings), raise_server_exceptions=False)

    return make


def test_with_a_replica_reads_use_their_own_pool(
    replica_client: Callable[[str], TestClient], test_settings: Settings
) -> None:
    # The test database plays the "replica" here: only the wiring is checked
    with replica_client(test_settings.test_database_url) as client:
        state = client.app.state
        assert state.read_engine.sync_engine.pool is not state.engine.sync_engine.pool
        assert state.read_engine.get_execution_options()["postgresql_readonly"] is True


# ---------- /health ----------


def test_health_without_a_replica_says_disabled(client: TestClient) -> None:
    assert client.get("/health").json()["replica"] == "disabled"


def test_health_with_a_reachable_replica(
    replica_client: Callable[[str], TestClient], test_settings: Settings
) -> None:
    with replica_client(test_settings.test_database_url) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["replica"] == "ok"


def test_health_with_a_replica_that_is_down_is_unhealthy(
    replica_client: Callable[[str], TestClient], caplog: pytest.LogCaptureFixture
) -> None:
    with replica_client(UNREACHABLE_REPLICA) as client:
        response = client.get("/health")

    # Reads would fail, so the app is not healthy (unlike a dead cache)
    assert response.status_code == 503
    assert response.json() | {"cache": "x"} == {
        "status": "unhealthy",
        "database": "ok",
        "replica": "down",
        "cache": "x",
    }
    assert "Read replica health check failed" in caplog.text
