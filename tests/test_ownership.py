"""Ownership: only the owner (or an Admin) may read, change or delete a short URL.

    unknown or deleted code  -> 404 (nothing to protect)
    someone else's URL       -> 403 (it exists, but it is not yours)
    Admin                    -> allowed on every URL
    the redirect /{code}/    -> public, no token

Every 403 test also checks the database: a refused request must change nothing.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import URL, User, UserTier
from tests.conftest import AuthedUser

URLS = "/api/v1/urls"
TARGET = "https://www.python.org/"
OTHER_TARGET = "https://example.org/changed"

RunDb = Callable[..., Any]
MakeUser = Callable[..., AuthedUser]


def create(client: TestClient, user: AuthedUser, **body: Any) -> str:
    """Create a URL through the API and return its short code."""
    response = client.post(f"{URLS}/", json={"original_url": TARGET, **body}, headers=user.headers)
    assert response.status_code == 201, response.text
    return response.json()["short_code"]


def stored(run_db: RunDb, code: str) -> URL:
    async def load(session: AsyncSession) -> URL | None:
        return await session.scalar(select(URL).where(URL.short_code == code))

    url = run_db(load)
    assert url is not None
    return url


def error_code(response: Any) -> str:
    return response.json()["error"]["code"]


# ---------- someone else's URL: 403, and nothing changes ----------


def test_another_user_cannot_update_my_url(
    db_client: TestClient, make_user: MakeUser, run_db: RunDb
) -> None:
    alice, bob = make_user(), make_user()
    code = create(db_client, alice)
    before = stored(run_db, code)

    response = db_client.put(
        f"{URLS}/{code}/",
        json={"original_url": OTHER_TARGET, "is_active": False, "title": "hacked"},
        headers=bob.headers,
    )

    assert response.status_code == 403
    assert error_code(response) == "forbidden"
    after = stored(run_db, code)
    assert (after.original_url, after.is_active, after.title) == (TARGET, True, None)
    assert after.updated_at == before.updated_at


def test_another_user_cannot_delete_my_url(
    db_client: TestClient, make_user: MakeUser, run_db: RunDb
) -> None:
    alice, bob = make_user(), make_user()
    code = create(db_client, alice)

    response = db_client.delete(f"{URLS}/{code}/", headers=bob.headers)

    assert response.status_code == 403
    assert error_code(response) == "forbidden"
    assert stored(run_db, code).deleted_at is None
    assert db_client.get(f"/{code}/").status_code == 302  # still redirects


def test_another_user_cannot_read_my_url(db_client: TestClient, make_user: MakeUser) -> None:
    alice, bob = make_user(), make_user()
    code = create(db_client, alice)

    response = db_client.get(f"{URLS}/{code}/", headers=bob.headers)

    assert response.status_code == 403
    assert TARGET not in response.text  # nothing of the URL leaks in the error


def test_a_premium_user_is_not_an_admin(db_client: TestClient, make_user: MakeUser) -> None:
    alice, premium = make_user(), make_user(UserTier.PREMIUM)
    code = create(db_client, alice)

    assert db_client.get(f"{URLS}/{code}/", headers=premium.headers).status_code == 403
    assert db_client.delete(f"{URLS}/{code}/", headers=premium.headers).status_code == 403


def test_a_switched_off_url_is_still_protected(db_client: TestClient, make_user: MakeUser) -> None:
    alice, bob = make_user(), make_user()
    code = create(db_client, alice)
    db_client.put(f"{URLS}/{code}/", json={"is_active": False}, headers=alice.headers)

    # Switched off is not deleted: it still belongs to Alice
    assert (
        db_client.put(f"{URLS}/{code}/", json={"is_active": True}, headers=bob.headers).status_code
        == 403
    )


def test_the_list_shows_only_my_own_urls(db_client: TestClient, make_user: MakeUser) -> None:
    alice, bob = make_user(), make_user()
    mine = create(db_client, alice)
    create(db_client, bob)

    items = db_client.get(f"{URLS}/", headers=alice.headers).json()["items"]

    assert [item["short_code"] for item in items] == [mine]


# ---------- unknown or deleted: 404 ----------


@pytest.mark.parametrize(
    ("method", "body"),
    [
        ("get", None),
        ("put", {"title": "x"}),
        ("put", {"is_active": True}),  # the "may come back to life" path checks first
        ("delete", None),
    ],
    ids=["get", "put", "put_switch_on", "delete"],
)
def test_an_unknown_code_is_404(
    db_client: TestClient, make_user: MakeUser, method: str, body: dict[str, Any] | None
) -> None:
    user = make_user(UserTier.ADMIN)  # even an Admin: there is nothing there
    kwargs: dict[str, Any] = {"headers": user.headers}
    if body is not None:
        kwargs["json"] = body

    response = getattr(db_client, method)(f"{URLS}/nope123/", **kwargs)

    assert response.status_code == 404
    assert error_code(response) == "url_not_found"


@pytest.mark.parametrize("who", ["owner", "other", "admin"])
def test_a_deleted_url_is_404_for_everyone(
    db_client: TestClient, make_user: MakeUser, who: str
) -> None:
    alice = make_user()
    code = create(db_client, alice)
    assert db_client.delete(f"{URLS}/{code}/", headers=alice.headers).status_code == 204
    caller = {"owner": alice, "other": make_user(), "admin": make_user(UserTier.ADMIN)}[who]

    assert db_client.get(f"{URLS}/{code}/", headers=caller.headers).status_code == 404
    assert db_client.delete(f"{URLS}/{code}/", headers=caller.headers).status_code == 404


# ---------- Admin may manage every URL ----------


def test_an_admin_can_read_update_and_delete_any_url(
    db_client: TestClient, make_user: MakeUser, run_db: RunDb
) -> None:
    alice, admin = make_user(), make_user(UserTier.ADMIN)
    code = create(db_client, alice)

    read = db_client.get(f"{URLS}/{code}/", headers=admin.headers)
    update = db_client.put(f"{URLS}/{code}/", json={"title": "checked"}, headers=admin.headers)
    delete = db_client.delete(f"{URLS}/{code}/", headers=admin.headers)

    assert (read.status_code, update.status_code, delete.status_code) == (200, 200, 204)
    assert read.json()["owner"]["id"] == alice.id
    url = stored(run_db, code)
    assert url.owner_id == alice.id  # the Admin never becomes the owner
    assert url.title == "checked"
    assert url.deleted_at is not None


def test_an_admin_list_shows_only_the_admins_own_urls(
    db_client: TestClient, make_user: MakeUser
) -> None:
    alice, admin = make_user(), make_user(UserTier.ADMIN)
    create(db_client, alice)

    assert db_client.get(f"{URLS}/", headers=admin.headers).json()["items"] == []


def test_a_demoted_admin_loses_the_override_at_once(
    db_client: TestClient, make_user: MakeUser, run_db: RunDb
) -> None:
    alice, admin = make_user(), make_user(UserTier.ADMIN)
    code = create(db_client, alice)

    run_db(
        lambda session: session.execute(
            update(User).where(User.id == admin.id).values(tier=UserTier.FREE)
        )
    )

    # Same token as before: the tier is read from the database, not from the token
    assert db_client.get(f"{URLS}/{code}/", headers=admin.headers).status_code == 403


# ---------- the owner and the public ----------


def test_the_owner_can_do_everything(db_client: TestClient, make_user: MakeUser) -> None:
    alice = make_user()
    code = create(db_client, alice)

    assert db_client.get(f"{URLS}/{code}/", headers=alice.headers).status_code == 200
    assert (
        db_client.put(f"{URLS}/{code}/", json={"title": "mine"}, headers=alice.headers).status_code
        == 200
    )
    assert db_client.delete(f"{URLS}/{code}/", headers=alice.headers).status_code == 204


def test_the_redirect_stays_public(db_client: TestClient, make_user: MakeUser) -> None:
    code = create(db_client, make_user())

    response = db_client.get(f"/{code}/")  # no token at all

    assert response.status_code == 302
    assert response.headers["location"] == TARGET


@pytest.mark.parametrize("field", ["owner_id", "click_count", "short_code"])
def test_create_refuses_fields_only_the_server_sets(
    db_client: TestClient, make_user: MakeUser, field: str
) -> None:
    other = make_user()
    user = make_user()
    value = other.id if field == "owner_id" else 999 if field == "click_count" else "mine123"

    response = db_client.post(
        f"{URLS}/", json={"original_url": TARGET, field: value}, headers=user.headers
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("owner_id", 1),
        ("click_count", 0),
        ("short_code", "new1234"),
        ("custom_alias", "new-alias"),
        ("deleted_at", datetime(2020, 1, 1, tzinfo=UTC).isoformat()),
    ],
)
def test_update_refuses_fields_only_the_server_sets(
    db_client: TestClient, make_user: MakeUser, run_db: RunDb, field: str, value: Any
) -> None:
    user = make_user()
    code = create(db_client, user)

    response = db_client.put(f"{URLS}/{code}/", json={field: value}, headers=user.headers)

    assert response.status_code == 422
    assert stored(run_db, code).owner_id == user.id
