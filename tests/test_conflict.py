"""Database constraint errors become clean JSON responses."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from app.api.dependencies import URLRepoDep, UserRepoDep
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


class FakePostgresError(Exception):
    """Stands in for the driver error: only the SQLSTATE code matters here."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


@pytest.mark.parametrize("sqlstate", ["40P01", "40001"])  # deadlock, serialization failure
def test_deadlock_returns_503_that_can_be_retried(
    app: FastAPI, db_client: TestClient, sqlstate: str
) -> None:
    @app.post("/_test/deadlock")
    async def deadlock() -> None:
        raise DBAPIError("UPDATE ...", {}, FakePostgresError(sqlstate))

    response = db_client.post("/_test/deadlock")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json()["error"]["code"] == "try_again"


def test_other_database_errors_stay_500(app: FastAPI, db_client: TestClient) -> None:
    @app.post("/_test/db-down")
    async def db_down() -> None:
        raise DBAPIError("SELECT 1", {}, FakePostgresError("08006"))  # connection failure

    response = db_client.post("/_test/db-down")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_server_error"


def test_other_constraint_errors_stay_500(app: FastAPI, db_client: TestClient) -> None:
    # A foreign key pointing nowhere is a bug in OUR code, not the client's fault
    @app.post("/_test/orphan-url")
    async def orphan_url(urls: URLRepoDep) -> None:
        await urls.add(URL(original_url="https://x.com/", short_code="orphan", owner_id=999_999))

    response = db_client.post("/_test/orphan-url")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_server_error"
