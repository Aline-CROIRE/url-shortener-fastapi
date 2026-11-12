import re
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import get_url_service

SHORT_CODE_PATTERN = re.compile(r"^[A-Za-z0-9]{7}$")


def create_url(client: TestClient, url: str = "https://www.python.org/about/") -> dict:
    response = client.post("/api/v1/urls", json={"original_url": url})
    assert response.status_code == 201
    return response.json()


def assert_error_shape(body: dict, code: str) -> None:
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str)


# ---------- POST /api/v1/urls ----------


def test_create_short_url_returns_201(client: TestClient) -> None:
    response = client.post("/api/v1/urls", json={"original_url": "https://www.python.org/about/"})

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"short_code", "original_url", "short_url", "created_at"}
    assert SHORT_CODE_PATTERN.match(body["short_code"])
    assert body["original_url"] == "https://www.python.org/about/"
    # base_url "http://short.test/" -> trailing slash removed, then "/" + code
    assert body["short_url"] == f"http://short.test/{body['short_code']}"
    # created_at must be a timezone-aware (UTC) time
    assert datetime.fromisoformat(body["created_at"]).utcoffset() is not None


def test_create_gives_different_codes(client: TestClient) -> None:
    codes = {create_url(client)["short_code"] for _ in range(20)}
    assert len(codes) == 20


def test_create_with_invalid_url_returns_422(client: TestClient) -> None:
    response = client.post("/api/v1/urls", json={"original_url": "not-a-url"})

    assert response.status_code == 422
    body = response.json()
    assert_error_shape(body, "validation_error")
    assert body["error"]["details"][0]["field"] == "body.original_url"


def test_create_without_body_returns_422(client: TestClient) -> None:
    response = client.post("/api/v1/urls")

    assert response.status_code == 422
    assert_error_shape(response.json(), "validation_error")


# ---------- GET /api/v1/urls/{short_code} ----------


def test_get_url_info_returns_200(client: TestClient) -> None:
    created = create_url(client)

    response = client.get(f"/api/v1/urls/{created['short_code']}")

    assert response.status_code == 200
    assert response.json() == created


def test_get_unknown_code_returns_404(client: TestClient) -> None:
    response = client.get("/api/v1/urls/unknown")

    assert response.status_code == 404
    body = response.json()
    assert_error_shape(body, "url_not_found")
    assert body["error"]["details"] == {"short_code": "unknown"}


# ---------- GET /{short_code} (redirect) ----------


def test_redirect_returns_302_with_location(client: TestClient) -> None:
    created = create_url(client, "https://fastapi.tiangolo.com/")

    response = client.get(f"/{created['short_code']}")

    assert response.status_code == 302
    assert response.headers["location"] == "https://fastapi.tiangolo.com/"


def test_redirect_unknown_code_returns_404(client: TestClient) -> None:
    response = client.get("/unknown")

    assert response.status_code == 404
    assert_error_shape(response.json(), "url_not_found")


def test_badly_formed_code_returns_422(client: TestClient) -> None:
    # Wrong length or characters: rejected before any storage lookup
    for path in ("/ab", "/api/v1/urls/ab", "/elevenchars", "/bad!code"):
        response = client.get(path)
        assert response.status_code == 422, path
        assert_error_shape(response.json(), "validation_error")


def test_redirect_route_does_not_hide_docs(client: TestClient) -> None:
    # The catch-all /{short_code} is included last, so /docs still works
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


# ---------- Error shape and middleware ----------


def test_unknown_route_uses_error_shape(client: TestClient) -> None:
    response = client.get("/api/v1/does/not/exist")

    assert response.status_code == 404
    assert_error_shape(response.json(), "not_found")


def test_wrong_method_returns_405(client: TestClient) -> None:
    response = client.delete("/api/v1/urls")

    assert response.status_code == 405
    assert_error_shape(response.json(), "method_not_allowed")


def test_unexpected_error_returns_500_without_details(app: FastAPI, client: TestClient) -> None:
    class BrokenService:
        async def get_url(self, short_code: str) -> None:
            raise ValueError("database password is hunter2")

    app.dependency_overrides[get_url_service] = lambda: BrokenService()

    response = client.get("/api/v1/urls/abc1234")

    assert response.status_code == 500
    assert_error_shape(response.json(), "internal_server_error")
    assert "hunter2" not in response.text  # internal details never reach the user
    assert response.headers["x-request-id"]


def test_responses_have_request_id_header(client: TestClient) -> None:
    response = client.get("/health")
    assert len(response.headers["x-request-id"]) == 32  # uuid4 hex


def test_incoming_request_id_is_reused(client: TestClient) -> None:
    response = client.get("/health", headers={"X-Request-ID": "trace-123"})
    assert response.headers["x-request-id"] == "trace-123"
