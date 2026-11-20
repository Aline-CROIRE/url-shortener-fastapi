"""Tier rules: Free = at most 10 active URLs and no custom alias; Premium/Admin = no limits.

"Active" means live: switched on, not deleted and not expired. The limit is a rule
about the ACCOUNT ("a Free account never has more than 10 live URLs"), so every way
of getting an 11th live URL is refused, not only POST: switching an old URL back on
or giving an expired one a new date counts too.

The tier is always read from the database, never from the token: a promotion or a
downgrade counts at once, with the same token.
"""

import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.permissions import require_premium, require_tier
from app.core.exceptions import InsufficientTierError, InvalidTokenError
from app.models import URL, User, UserTier
from app.repositories import TagRepository, URLRepository, UserRepository
from app.repositories.url import utc_now
from app.schemas.url import URLCreate
from app.services.urls import URLService
from tests.conftest import AuthedUser

URLS = "/api/v1/urls"
TARGET = "https://www.python.org/"
FREE_LIMIT = 10

RunDb = Callable[..., Any]
MakeUser = Callable[..., AuthedUser]


# ---------- helpers ----------


def seed_urls(run_db: RunDb, owner_id: int, count: int, **fields: Any) -> list[str]:
    """Insert URLs straight into the database (any state, even expired) and return their codes."""
    codes = [secrets.token_hex(4) for _ in range(count)]

    async def insert(session: AsyncSession) -> None:
        session.add_all(
            URL(original_url=TARGET, short_code=code, owner_id=owner_id, **fields) for code in codes
        )

    run_db(insert)
    return codes


def live_count(run_db: RunDb, owner_id: int) -> int:
    async def count(session: AsyncSession) -> int:
        statement = select(func.count()).where(URL.owner_id == owner_id, URL.is_live(utc_now()))
        return await session.scalar(statement) or 0

    return run_db(count)


def set_tier(run_db: RunDb, user_id: int, tier: UserTier) -> None:
    run_db(
        lambda session: session.execute(update(User).where(User.id == user_id).values(tier=tier))
    )


def load(run_db: RunDb, code: str) -> URL:
    url = run_db(lambda session: session.scalar(select(URL).where(URL.short_code == code)))
    assert url is not None
    return url


def create(client: TestClient, user: AuthedUser, **body: Any) -> Any:
    return client.post(f"{URLS}/", json={"original_url": TARGET, **body}, headers=user.headers)


def error_code(response: Any) -> str:
    return response.json()["error"]["code"]


PAST = datetime(2020, 1, 1, tzinfo=UTC)


def future() -> str:
    return (datetime.now(UTC) + timedelta(days=30)).isoformat()


# ---------- require_tier / require_premium ----------


def user_with(tier: UserTier) -> User:
    return User(id=1, email="t@example.com", username="t", hashed_password="!", tier=tier)


@pytest.mark.anyio
@pytest.mark.parametrize("tier", [UserTier.PREMIUM, UserTier.ADMIN])
async def test_require_premium_lets_premium_and_admin_through(tier: UserTier) -> None:
    user = user_with(tier)

    assert await require_premium(user) is user


@pytest.mark.anyio
async def test_require_premium_refuses_free() -> None:
    with pytest.raises(InsufficientTierError) as error:
        await require_premium(user_with(UserTier.FREE))

    assert error.value.status_code == 403


@pytest.mark.anyio
async def test_require_tier_allows_only_the_listed_tiers() -> None:
    admin_only = require_tier(UserTier.ADMIN)

    assert await admin_only(user_with(UserTier.ADMIN)) is not None
    for tier in (UserTier.FREE, UserTier.PREMIUM):
        with pytest.raises(InsufficientTierError):
            await admin_only(user_with(tier))


def test_require_tier_needs_at_least_one_tier() -> None:
    with pytest.raises(ValueError, match="at least one tier"):
        require_tier()


# ---------- Free: at most 10 active URLs (brief test #8) ----------


def test_the_11th_url_of_a_free_user_is_403(
    db_client: TestClient, make_user: MakeUser, run_db: RunDb
) -> None:
    free = make_user()
    for number in range(FREE_LIMIT):
        assert create(db_client, free).status_code == 201, number

    response = create(db_client, free)

    assert response.status_code == 403
    assert error_code(response) == "free_tier_limit"
    message = response.json()["error"]["message"]
    assert "10" in message and "Premium" in message  # says what the limit is and the way out
    assert response.json()["error"]["details"] == {"limit": FREE_LIMIT}
    assert live_count(run_db, free.id) == FREE_LIMIT  # nothing was saved


@pytest.mark.parametrize(
    "state",
    [
        {"is_active": False},  # switched off
        {"deleted_at": PAST},  # soft-deleted
        {"expires_at": PAST},  # expired
    ],
    ids=["switched_off", "deleted", "expired"],
)
def test_urls_that_are_not_live_do_not_count(
    db_client: TestClient, make_user: MakeUser, run_db: RunDb, state: dict[str, Any]
) -> None:
    free = make_user()
    seed_urls(run_db, free.id, FREE_LIMIT, **state)

    assert create(db_client, free).status_code == 201


def test_other_users_urls_do_not_count(
    db_client: TestClient, make_user: MakeUser, run_db: RunDb
) -> None:
    free, other = make_user(), make_user()
    seed_urls(run_db, other.id, FREE_LIMIT)

    assert create(db_client, free).status_code == 201


def test_deleting_one_makes_room_again(
    db_client: TestClient, make_user: MakeUser, run_db: RunDb
) -> None:
    free = make_user()
    codes = seed_urls(run_db, free.id, FREE_LIMIT)
    assert create(db_client, free).status_code == 403

    assert db_client.delete(f"{URLS}/{codes[0]}/", headers=free.headers).status_code == 204

    assert create(db_client, free).status_code == 201


def test_switching_one_off_makes_room_again(
    db_client: TestClient, make_user: MakeUser, run_db: RunDb
) -> None:
    free = make_user()
    codes = seed_urls(run_db, free.id, FREE_LIMIT)

    off = db_client.put(f"{URLS}/{codes[0]}/", json={"is_active": False}, headers=free.headers)

    assert off.status_code == 200
    assert create(db_client, free).status_code == 201


# ---------- the limit cannot be dodged through PUT ----------


def test_switching_a_url_back_on_at_the_limit_is_403(
    db_client: TestClient, make_user: MakeUser, run_db: RunDb
) -> None:
    free = make_user()
    seed_urls(run_db, free.id, FREE_LIMIT)
    (off,) = seed_urls(run_db, free.id, 1, is_active=False)

    response = db_client.put(f"{URLS}/{off}/", json={"is_active": True}, headers=free.headers)

    assert response.status_code == 403
    assert error_code(response) == "free_tier_limit"
    assert load(run_db, off).is_active is False
    assert live_count(run_db, free.id) == FREE_LIMIT


@pytest.mark.parametrize("new_expiry", [None, "future"], ids=["remove_expiry", "later_date"])
def test_reviving_an_expired_url_at_the_limit_is_403(
    db_client: TestClient, make_user: MakeUser, run_db: RunDb, new_expiry: str | None
) -> None:
    free = make_user()
    seed_urls(run_db, free.id, FREE_LIMIT)
    (expired,) = seed_urls(run_db, free.id, 1, expires_at=PAST)
    value = future() if new_expiry else None

    response = db_client.put(f"{URLS}/{expired}/", json={"expires_at": value}, headers=free.headers)

    assert response.status_code == 403
    assert load(run_db, expired).expires_at == PAST
    assert live_count(run_db, free.id) == FREE_LIMIT


@pytest.mark.parametrize(
    "change",
    [
        {"title": "New title"},
        {"is_active": True},  # already on: nothing new becomes live
        {"tags": ["Marketing"]},
        {"original_url": "https://example.org/"},
    ],
    ids=["title", "already_active", "tags", "target"],
)
def test_editing_a_live_url_at_the_limit_is_allowed(
    db_client: TestClient, make_user: MakeUser, run_db: RunDb, change: dict[str, Any]
) -> None:
    free = make_user()
    codes = seed_urls(run_db, free.id, FREE_LIMIT)

    response = db_client.put(f"{URLS}/{codes[0]}/", json=change, headers=free.headers)

    assert response.status_code == 200  # no false 403: the number of live URLs stays 10


def test_editing_a_switched_off_url_at_the_limit_is_allowed(
    db_client: TestClient, make_user: MakeUser, run_db: RunDb
) -> None:
    free = make_user()
    seed_urls(run_db, free.id, FREE_LIMIT)
    (off,) = seed_urls(run_db, free.id, 1, is_active=False)

    response = db_client.put(f"{URLS}/{off}/", json={"title": "Later"}, headers=free.headers)

    assert response.status_code == 200
    assert load(run_db, off).is_active is False


def test_an_admin_cannot_push_a_free_account_over_the_limit(
    db_client: TestClient, make_user: MakeUser, run_db: RunDb
) -> None:
    free, admin = make_user(), make_user(UserTier.ADMIN)
    seed_urls(run_db, free.id, FREE_LIMIT)
    (off,) = seed_urls(run_db, free.id, 1, is_active=False)

    # The limit belongs to the OWNER's account, whoever makes the change
    response = db_client.put(f"{URLS}/{off}/", json={"is_active": True}, headers=admin.headers)

    assert response.status_code == 403
    assert error_code(response) == "free_tier_limit"


# ---------- Premium / Admin: no limit ----------


@pytest.mark.parametrize("tier", [UserTier.PREMIUM, UserTier.ADMIN])
def test_premium_and_admin_have_no_url_limit(
    db_client: TestClient, make_user: MakeUser, run_db: RunDb, tier: UserTier
) -> None:
    user = make_user(tier)
    seed_urls(run_db, user.id, FREE_LIMIT + 5)

    assert create(db_client, user).status_code == 201


def test_a_downgraded_user_keeps_their_urls_but_cannot_add_more(
    db_client: TestClient, make_user: MakeUser, run_db: RunDb
) -> None:
    user = make_user(UserTier.PREMIUM)
    codes = seed_urls(run_db, user.id, FREE_LIMIT + 2)

    set_tier(run_db, user.id, UserTier.FREE)  # same token as before

    assert create(db_client, user).status_code == 403
    assert live_count(run_db, user.id) == FREE_LIMIT + 2  # nothing is taken away
    edit = db_client.put(f"{URLS}/{codes[0]}/", json={"title": "still mine"}, headers=user.headers)
    assert edit.status_code == 200


def test_a_promotion_counts_at_once(
    db_client: TestClient, make_user: MakeUser, run_db: RunDb
) -> None:
    free = make_user()
    seed_urls(run_db, free.id, FREE_LIMIT)
    assert create(db_client, free).status_code == 403

    set_tier(run_db, free.id, UserTier.PREMIUM)  # same token as before

    assert create(db_client, free).status_code == 201


# ---------- custom aliases (brief tests #8 and #9) ----------


def test_a_free_user_cannot_choose_an_alias(
    db_client: TestClient, make_user: MakeUser, run_db: RunDb
) -> None:
    free = make_user()

    response = create(db_client, free, custom_alias="my-link")

    assert response.status_code == 403
    assert error_code(response) == "insufficient_tier"
    assert "Premium" in response.json()["error"]["message"]
    assert live_count(run_db, free.id) == 0  # nothing was saved
    assert db_client.get("/my-link/").status_code == 404


@pytest.mark.parametrize("tier", [UserTier.PREMIUM, UserTier.ADMIN])
def test_premium_and_admin_can_choose_an_alias(
    db_client: TestClient, make_user: MakeUser, tier: UserTier
) -> None:
    response = create(db_client, make_user(tier), custom_alias="my-link")

    assert response.status_code == 201
    assert response.json()["short_code"] == "my-link"


@pytest.mark.parametrize("alias", ["api", "API", "docs", "admin", "health"])
def test_a_reserved_alias_is_422_even_for_premium(
    db_client: TestClient, make_user: MakeUser, alias: str
) -> None:
    response = create(db_client, make_user(UserTier.PREMIUM), custom_alias=alias)

    assert response.status_code == 422


def test_a_taken_alias_is_still_409_for_premium(db_client: TestClient, make_user: MakeUser) -> None:
    first, second = make_user(UserTier.PREMIUM), make_user(UserTier.PREMIUM)
    assert create(db_client, first, custom_alias="taken").status_code == 201

    assert create(db_client, second, custom_alias="taken").status_code == 409


def test_a_promoted_user_can_choose_an_alias_with_the_same_token(
    db_client: TestClient, make_user: MakeUser, run_db: RunDb
) -> None:
    free = make_user()
    set_tier(run_db, free.id, UserTier.PREMIUM)

    assert create(db_client, free, custom_alias="now-ok").status_code == 201


# ---------- the owner disappears between the token check and the create ----------


@pytest.mark.anyio
async def test_a_user_deleted_after_the_token_check_cannot_create(db_session: AsyncSession) -> None:
    # get_current_user found the user, then the account was deleted before the
    # create locked its row: a clean 401, never a URL without an owner or a 500
    service = URLService(
        session=db_session,
        urls=URLRepository(db_session),
        users=UserRepository(db_session),
        tags=TagRepository(db_session),
    )

    with pytest.raises(InvalidTokenError):
        await service.create(URLCreate(original_url=TARGET), owner_id=2_000_000_000)
