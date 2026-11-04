from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import get_repository


def test_health_returns_200(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "storage": "memory"}


def test_health_is_outside_api_version_prefix(client: TestClient) -> None:
    assert client.get("/api/v1/health").status_code == 404


def test_health_returns_503_when_storage_is_down(app: FastAPI, client: TestClient) -> None:
    class DownRepository:
        async def ping(self) -> bool:
            return False

    app.dependency_overrides[get_repository] = lambda: DownRepository()

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "unhealthy"
