"""/api/v1/urls endpoints: create, read, update and soft delete the owner's URLs."""

import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.owner import DEMO_OWNER_EMAIL
from app.models import URL, Click, Tag, User
from app.repositories import URLRepository
from app.services import urls as url_service_module

RANDOM_CODE = re.compile(r"^[A-Za-z0-9]{7}$")
TARGET = "https://www.python.org/about/"
API = "/api/v1/urls"

RunDb = Callable[..., Any]


def create(client: TestClient, **body: Any) -> dict[str, Any]:
    response = client.post(API, json={"original_url": TARGET, **body})
    assert response.status_code == 201, response.text
    return response.json()


def error_code(response: Any) -> str:
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    return body["error"]["code"]


def count_rows(run_db: RunDb, model: type) -> int:
    async def work(session: AsyncSession) -> int:
        return await session.scalar(select(func.count()).select_from(model))

    return run_db(work)


@pytest.fixture
def other_owners_code(run_db: RunDb) -> str:
    """A URL that belongs to somebody else."""

    async def work(session: AsyncSession) -> str:
        stranger = User(email="stranger@example.com", username="stranger", hashed_password="h")
        session.add(URL(original_url=TARGET, short_code="theirs1", owner=stranger))
        return "theirs1"

    return run_db(work)


# ---------- POST /api/v1/urls ----------


def test_create_minimal(db_client: TestClient, owner_id: int) -> None:
    body = create(db_client)

    assert RANDOM_CODE.match(body["short_code"])
    assert body["short_url"] == f"http://short.test/{body['short_code']}"
    assert body["original_url"] == TARGET
    assert body["owner"] == {"id": owner_id, "username": "me"}
    assert body["tags"] == []
    assert body["custom_alias"] is None
    assert body["expires_at"] is None
    assert (body["is_active"], body["click_count"]) == (True, 0)
    assert datetime.fromisoformat(body["created_at"]).utcoffset() is not None


def test_create_with_alias_expiry_and_tags(db_client: TestClient, owner_id: int) -> None:
    expires = (datetime.now(UTC) + timedelta(days=30)).isoformat()

    body = create(
        db_client, custom_alias="py-about", expires_at=expires, tags=["marketing", "Launch"]
    )

    assert body["short_code"] == body["custom_alias"] == "py-about"
    assert body["short_url"] == "http://short.test/py-about"
    assert datetime.fromisoformat(body["expires_at"]) == datetime.fromisoformat(expires)
    # "marketing" reuses the seeded tag (its spelling kept); "Launch" is created
    assert [tag["name"] for tag in body["tags"]] == ["Marketing", "Launch"]


def test_created_link_redirects(db_client: TestClient, owner_id: int) -> None:
    code = create(db_client)["short_code"]

    response = db_client.get(f"/{code}")

    assert response.status_code == 302
    assert response.headers["location"] == TARGET


@pytest.mark.parametrize(
    "body",
    [
        {"original_url": "not-a-url"},
        {"original_url": TARGET, "custom_alias": "docs"},  # reserved
        {"original_url": TARGET, "custom_alias": "a b"},  # bad characters
        {"original_url": TARGET, "expires_at": "2020-01-01T00:00:00Z"},  # in the past
        {"original_url": TARGET, "short_code": "mine"},  # not allowed
        {},
    ],
)
def test_create_rejects_invalid_input(db_client: TestClient, owner_id: int, body: dict) -> None:
    response = db_client.post(API, json=body)

    assert response.status_code == 422
    assert error_code(response) == "validation_error"


def test_taken_alias_returns_409(db_client: TestClient, owner_id: int, run_db: RunDb) -> None:
    create(db_client, custom_alias="promo")

    response = db_client.post(API, json={"original_url": TARGET, "custom_alias": "promo"})

    assert response.status_code == 409
    assert error_code(response) == "alias_taken"
    assert response.json()["error"]["details"] == {"custom_alias": "promo"}
    assert count_rows(run_db, URL) == 1  # the failed attempt left nothing behind


def test_alias_cannot_take_an_existing_random_code(
    db_client: TestClient, owner_id: int
) -> None:
    random_code = create(db_client)["short_code"]

    response = db_client.post(API, json={"original_url": TARGET, "custom_alias": random_code})

    assert response.status_code == 409


def test_random_code_clash_retries_with_a_new_code(
    db_client: TestClient, owner_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(db_client, custom_alias="taken01")
    codes = iter(["taken01", "fresh01"])  # the first random code is already used
    monkeypatch.setattr(url_service_module, "generate_code", lambda: next(codes))

    body = create(db_client)

    assert body["short_code"] == "fresh01"


def test_gives_up_after_too_many_clashes(
    db_client: TestClient, owner_id: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    create(db_client, custom_alias="taken01")
    monkeypatch.setattr(url_service_module, "generate_code", lambda: "taken01")

    response = db_client.post(API, json={"original_url": TARGET})

    assert response.status_code == 500
    assert error_code(response) == "short_code_generation_failed"


def test_url_and_tags_are_saved_in_one_transaction(
    db_client: TestClient, owner_id: int, run_db: RunDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    tags_before = count_rows(run_db, Tag)
    original_add = URLRepository.add

    async def add_then_fail(self: URLRepository, url: URL) -> URL:
        await original_add(self, url)  # INSERT url + new tag + url_tags rows...
        raise RuntimeError("crash after the INSERTs")  # ...then something fails

    monkeypatch.setattr(URLRepository, "add", add_then_fail)

    response = db_client.post(API, json={"original_url": TARGET, "tags": ["BrandNewTag"]})

    assert response.status_code == 500
    assert count_rows(run_db, URL) == 0  # no URL...
    assert count_rows(run_db, Tag) == tags_before  # ...and no half-created tag


def test_trailing_slash_redirects_with_307(db_client: TestClient, owner_id: int) -> None:
    response = db_client.post(f"{API}/", json={"original_url": TARGET})

    assert response.status_code == 307  # 307 keeps the method (POST) and the body
    assert response.headers["location"].endswith(API)


# ---------- GET /api/v1/urls/{short_code} ----------


def test_get_returns_owner_tags_and_click_count(db_client: TestClient, owner_id: int) -> None:
    code = create(db_client, tags=["Email", "Ads"])["short_code"]
    db_client.get(f"/{code}")
    db_client.get(f"/{code}")

    response = db_client.get(f"{API}/{code}")

    assert response.status_code == 200
    body = response.json()
    assert body["owner"] == {"id": owner_id, "username": "me"}
    assert sorted(tag["name"] for tag in body["tags"]) == ["Ads", "Email"]
    assert body["click_count"] == 2


def test_get_unknown_returns_404(db_client: TestClient, owner_id: int) -> None:
    response = db_client.get(f"{API}/nothing")

    assert response.status_code == 404
    assert error_code(response) == "url_not_found"


def test_badly_formed_code_returns_422(db_client: TestClient, owner_id: int) -> None:
    # Wrong length or characters: rejected before any database lookup
    for path in ("/ab", f"{API}/ab", "/elevenchars", "/bad!code"):
        response = db_client.get(path)
        assert response.status_code == 422, path
        assert error_code(response) == "validation_error"


def test_someone_elses_url_is_not_found(
    db_client: TestClient, owner_id: int, other_owners_code: str
) -> None:
    # 404, not 403: we do not even reveal that the code exists
    assert db_client.get(f"{API}/{other_owners_code}").status_code == 404
    assert db_client.put(f"{API}/{other_owners_code}", json={"title": "x"}).status_code == 404
    assert db_client.delete(f"{API}/{other_owners_code}").status_code == 404


# ---------- PUT /api/v1/urls/{short_code} ----------


def test_put_changes_only_the_fields_sent(db_client: TestClient, owner_id: int) -> None:
    created = create(db_client, tags=["Email"])

    response = db_client.put(f"{API}/{created['short_code']}", json={"title": "Python"})

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Python"
    assert body["original_url"] == created["original_url"]  # untouched
    assert [tag["name"] for tag in body["tags"]] == ["Email"]  # untouched


def test_put_replaces_tags_and_url(db_client: TestClient, owner_id: int) -> None:
    code = create(db_client, tags=["Email", "Ads"])["short_code"]

    body = db_client.put(
        f"{API}/{code}", json={"tags": ["Social"], "original_url": "https://fastapi.tiangolo.com/"}
    ).json()

    assert [tag["name"] for tag in body["tags"]] == ["Social"]
    assert body["original_url"] == "https://fastapi.tiangolo.com/"


def test_put_null_expiry_removes_it(db_client: TestClient, owner_id: int) -> None:
    expires = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    code = create(db_client, expires_at=expires)["short_code"]

    body = db_client.put(f"{API}/{code}", json={"expires_at": None}).json()

    assert body["expires_at"] is None


def test_switched_off_link_stops_redirecting(db_client: TestClient, owner_id: int) -> None:
    code = create(db_client)["short_code"]

    db_client.put(f"{API}/{code}", json={"is_active": False})

    assert db_client.get(f"/{code}").status_code == 410


@pytest.mark.parametrize(
    "body",
    [{"original_url": None}, {"is_active": None}, {"short_code": "new"}, {"custom_alias": "new"}],
)
def test_put_rejects_invalid_changes(db_client: TestClient, owner_id: int, body: dict) -> None:
    code = create(db_client)["short_code"]

    response = db_client.put(f"{API}/{code}", json=body)

    assert response.status_code == 422


# ---------- DELETE /api/v1/urls/{short_code} (soft delete) ----------


def test_delete_is_a_soft_delete(db_client: TestClient, owner_id: int, run_db: RunDb) -> None:
    code = create(db_client, custom_alias="bye-bye")["short_code"]
    db_client.get(f"/{code}")  # one click before deleting

    response = db_client.delete(f"{API}/{code}")

    assert response.status_code == 204
    assert response.content == b""
    assert db_client.get(f"{API}/{code}").status_code == 404  # hidden from the owner
    redirect = db_client.get(f"/{code}")
    assert redirect.status_code == 410
    assert error_code(redirect) == "url_deleted"
    assert count_rows(run_db, URL) == 1  # the row is still there...
    assert count_rows(run_db, Click) == 1  # ...and so is its history


def test_deleted_code_is_never_given_to_someone_else(db_client: TestClient, owner_id: int) -> None:
    code = create(db_client, custom_alias="promo")["short_code"]
    db_client.delete(f"{API}/{code}")

    response = db_client.post(API, json={"original_url": "https://evil.example/", "custom_alias": code})

    assert response.status_code == 409  # no link hijacking


def test_delete_twice_returns_404(db_client: TestClient, owner_id: int) -> None:
    code = create(db_client)["short_code"]

    assert db_client.delete(f"{API}/{code}").status_code == 204
    assert db_client.delete(f"{API}/{code}").status_code == 404


# ---------- the temporary owner (TODO(module-7)) ----------


@pytest.fixture
def remove_demo_owner(test_settings: Any) -> Iterator[None]:
    """The demo owner is created with the app's own session (really committed),
    so delete it again after the test."""
    yield

    async def cleanup() -> None:
        engine = create_async_engine(test_settings.test_database_url, poolclass=NullPool)
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM users WHERE email = :email"), {"email": DEMO_OWNER_EMAIL}
            )
        await engine.dispose()

    import asyncio

    asyncio.run(cleanup())


def test_demo_owner_is_created_once_and_cached(
    app: FastAPI, db_client: TestClient, remove_demo_owner: None
) -> None:
    # No owner_id fixture here: the real temporary mechanism runs
    assert db_client.get(f"{API}/nothing").status_code == 404
    first_id = app.state.demo_owner_id
    assert db_client.get(f"{API}/nothing").status_code == 404

    assert isinstance(first_id, int)
    assert app.state.demo_owner_id == first_id  # cached: not looked up again
