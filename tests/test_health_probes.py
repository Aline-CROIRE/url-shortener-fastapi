"""Health probes: /health/live (is the process alive?) and /health/ready (can it serve?)."""

import asyncio
import time
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import get_redirect_cache
from app.api.routes import health as health_routes


class StubCache:
    """A redirect cache whose PING answers what the test wants."""

    def __init__(self, answer: bool | None = True, delay: float = 0.0) -> None:
        self.answer = answer
        self.delay = delay

    async def ping(self) -> bool | None:
        await asyncio.sleep(self.delay)
        return self.answer


@pytest.fixture
def cache(app: FastAPI) -> StubCache:
    stub = StubCache()
    app.dependency_overrides[get_redirect_cache] = lambda: stub
    return stub


# ---------- liveness ----------


def test_live_answers_without_checking_anything(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, cache: StubCache
) -> None:
    async def database_down(session: object) -> None:
        raise ConnectionRefusedError("PostgreSQL is down")

    monkeypatch.setattr(health_routes, "ping_database", database_down)
    cache.answer = False

    response = client.get("/health/live")

    # Alive = the process answers. Restarting it would not bring the DB back.
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


# ---------- readiness (/health and /health/ready are the same check) ----------


@pytest.mark.parametrize("path", ["/health", "/health/ready"])
def test_ready_when_database_and_redis_answer(
    client: TestClient, cache: StubCache, path: str
) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "database": "ok",
        "replica": "disabled",
        "cache": "ok",
    }


@pytest.mark.parametrize("path", ["/health", "/health/ready"])
def test_redis_down_is_not_ready(client: TestClient, cache: StubCache, path: str) -> None:
    cache.answer = False

    response = client.get(path)

    # Module 8: Redis is also the job queue; without it every click is lost
    assert response.status_code == 503
    assert response.json()["status"] == "unhealthy"
    assert response.json()["cache"] == "down"


def test_without_redis_configured_the_app_is_still_ready(client: TestClient) -> None:
    response = client.get("/health/ready")  # tests run with REDIS_URL empty

    assert response.status_code == 200
    assert response.json()["cache"] == "disabled"


def test_a_hanging_database_fails_the_check_quickly(
    client: TestClient, cache: StubCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hangs(session: object) -> None:
        await asyncio.sleep(30)

    monkeypatch.setattr(health_routes, "ping_database", hangs)
    monkeypatch.setattr(health_routes, "CHECK_TIMEOUT_SECONDS", 0.2)

    start = time.perf_counter()
    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["database"] == "down"
    assert time.perf_counter() - start < 5  # an orchestrator's probe would time out first


def test_a_hanging_redis_fails_the_check_quickly(
    client: TestClient, cache: StubCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache.delay = 30
    monkeypatch.setattr(health_routes, "CHECK_TIMEOUT_SECONDS", 0.2)

    start = time.perf_counter()
    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["cache"] == "down"
    assert time.perf_counter() - start < 5


def test_health_never_shows_error_details(
    client: TestClient, cache: StubCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(session: object) -> Any:
        raise ConnectionRefusedError("password authentication failed for user postgres")

    monkeypatch.setattr(health_routes, "ping_database", broken)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert "password" not in response.text  # only "ok" / "down" / "disabled"


@pytest.mark.parametrize("path", ["/health/live", "/health/ready", "/health"])
def test_probes_are_never_cached(client: TestClient, cache: StubCache, path: str) -> None:
    # A proxy that cached "healthy" would hide an outage
    assert client.get(path).headers["cache-control"] == "no-store"
