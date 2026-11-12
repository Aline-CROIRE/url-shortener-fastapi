from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.health import database_is_up


def test_health_returns_200(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "database": "ok"}


def test_health_is_outside_api_version_prefix(client: TestClient) -> None:
    assert client.get("/api/v1/health").status_code == 404


def test_health_returns_503_when_database_is_down(app: FastAPI, client: TestClient) -> None:
    app.dependency_overrides[database_is_up] = lambda: False

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "unhealthy", "database": "down"}
