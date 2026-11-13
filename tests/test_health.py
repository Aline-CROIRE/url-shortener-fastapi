import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.api.routes import health as health_routes
from app.api.routes.health import database_is_up


def test_health_returns_200(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "healthy",
        "database": "ok",
        "replica": "disabled",
        "cache": "disabled",
    }


def test_health_is_outside_api_version_prefix(client: TestClient) -> None:
    assert client.get("/api/v1/health").status_code == 404


def test_health_returns_503_when_database_is_down(app: FastAPI, client: TestClient) -> None:
    app.dependency_overrides[database_is_up] = lambda: False

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unhealthy",
        "database": "down",
        "replica": "disabled",
        "cache": "disabled",
    }


def test_real_database_failure_is_reported_and_logged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken_ping(session: object) -> None:
        raise SQLAlchemyError("connection refused")

    monkeypatch.setattr(health_routes, "ping_database", broken_ping)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "unhealthy",
        "database": "down",
        "replica": "disabled",
        "cache": "disabled",
    }
    assert "Database health check failed" in caplog.text  # the failure is not hidden
