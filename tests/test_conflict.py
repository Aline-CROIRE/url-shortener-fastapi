"""Database constraint errors become clean JSON responses."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.dependencies import UserRepoDep, URLRepoDep
from app.models import URL, User


def test_duplicate_unique_value_returns_409(app: FastAPI, db_client: TestClient) -> None:
    @app.post("/_test/duplicate-user")
    async def duplicate_user(users: UserRepoDep) -> None:
        await users.add(User(email="dup@example.com", username="first", hashed_password="h"))
        await users.add(User(email="dup@example.com", username="second", hashed_password="h"))

    response = db_client.post("/_test/duplicate-user")

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "conflict",
            "message": "This value already exists.",
            "details": {"constraint": "uq_users_email"},
        }
    }


def test_other_constraint_errors_stay_500(app: FastAPI, db_client: TestClient) -> None:
    # A foreign key pointing nowhere is a bug in OUR code, not the client's fault
    @app.post("/_test/orphan-url")
    async def orphan_url(urls: URLRepoDep) -> None:
        await urls.add(URL(original_url="https://x.com/", short_code="orphan", owner_id=999_999))

    response = db_client.post("/_test/orphan-url")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_server_error"
