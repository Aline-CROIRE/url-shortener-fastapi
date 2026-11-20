"""Analytics are a Premium feature, and only for your own URLs (brief test #11).

Free (even the owner)       -> 403 insufficient_tier (checked before the code)
Premium/Admin, own URL      -> 200
Premium, someone else's URL -> 403 forbidden
Admin, any URL              -> 200
unknown or deleted code     -> 404 (for Premium/Admin)
"""

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.models import User, UserTier
from tests.conftest import AuthedUser

URLS = "/api/v1/urls"
TARGET = "https://www.python.org/"

RunDb = Callable[..., Any]
MakeUser = Callable[..., AuthedUser]


def create(client: TestClient, user: AuthedUser) -> str:
    response = client.post(f"{URLS}/", json={"original_url": TARGET}, headers=user.headers)
    assert response.status_code == 201, response.text
    return response.json()["short_code"]


def analytics(client: TestClient, code: str, user: AuthedUser | None = None) -> Any:
    headers = user.headers if user else {}
    return client.get(f"/api/v1/analytics/{code}/", headers=headers)


def error_code(response: Any) -> str:
    return response.json()["error"]["code"]


def test_a_free_owner_gets_403(db_client: TestClient, make_user: MakeUser) -> None:
    free = make_user()
    code = create(db_client, free)

    response = analytics(db_client, code, free)

    assert response.status_code == 403
    assert error_code(response) == "insufficient_tier"
    assert "Premium" in response.json()["error"]["message"]


@pytest.mark.parametrize("tier", [UserTier.PREMIUM, UserTier.ADMIN])
def test_a_premium_or_admin_owner_gets_200(
    db_client: TestClient, make_user: MakeUser, tier: UserTier
) -> None:
    owner = make_user(tier)
    code = create(db_client, owner)
    db_client.get(f"/{code}/")  # one visit

    response = analytics(db_client, code, owner)

    assert response.status_code == 200
    assert response.json()["total_clicks"] == 1


def test_a_premium_user_cannot_see_someone_elses_analytics(
    db_client: TestClient, make_user: MakeUser
) -> None:
    owner, other = make_user(UserTier.PREMIUM), make_user(UserTier.PREMIUM)
    code = create(db_client, owner)

    response = analytics(db_client, code, other)

    assert response.status_code == 403
    assert error_code(response) == "forbidden"


def test_an_admin_can_see_any_urls_analytics(db_client: TestClient, make_user: MakeUser) -> None:
    free, admin = make_user(), make_user(UserTier.ADMIN)
    code = create(db_client, free)  # the owner is Free; the Admin may still look

    assert analytics(db_client, code, admin).status_code == 200


@pytest.mark.parametrize("tier", [UserTier.PREMIUM, UserTier.ADMIN])
def test_an_unknown_code_is_404_for_premium_and_admin(
    db_client: TestClient, make_user: MakeUser, tier: UserTier
) -> None:
    response = analytics(db_client, "nope123", make_user(tier))

    assert response.status_code == 404


def test_a_free_user_learns_nothing_about_codes(db_client: TestClient, make_user: MakeUser) -> None:
    free = make_user()
    existing = create(db_client, make_user(UserTier.PREMIUM))

    # Same 403 for an existing and an unknown code: the tier is checked first
    known = analytics(db_client, existing, free)
    unknown = analytics(db_client, "nope123", free)

    assert known.status_code == unknown.status_code == 403
    assert known.json() == unknown.json()


def test_a_deleted_url_has_no_analytics(db_client: TestClient, make_user: MakeUser) -> None:
    owner = make_user(UserTier.PREMIUM)
    code = create(db_client, owner)
    db_client.delete(f"{URLS}/{code}/", headers=owner.headers)

    assert analytics(db_client, code, owner).status_code == 404


def test_without_a_token_it_is_401(db_client: TestClient, make_user: MakeUser) -> None:
    code = create(db_client, make_user(UserTier.PREMIUM))

    response = analytics(db_client, code)

    assert response.status_code == 401
    assert error_code(response) == "not_authenticated"


def test_a_downgrade_counts_at_once(
    db_client: TestClient, make_user: MakeUser, run_db: RunDb
) -> None:
    owner = make_user(UserTier.PREMIUM)
    code = create(db_client, owner)
    assert analytics(db_client, code, owner).status_code == 200

    run_db(
        lambda session: session.execute(
            update(User).where(User.id == owner.id).values(tier=UserTier.FREE)
        )
    )

    assert analytics(db_client, code, owner).status_code == 403  # same token, fresh tier
