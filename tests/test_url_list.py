"""GET /api/v1/urls: keyset pagination, sorting and filtering."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import URL, User
from app.repositories import TagRepository
from app.repositories.url import URLSort
from app.services.pagination import encode_cursor

API = "/api/v1/urls"
NOW = datetime.now(UTC)
_numbers = count(1)

RunDb = Callable[..., Any]


def seed(run_db: RunDb, owner_id: int, *rows: dict[str, Any]) -> list[str]:
    """Insert URLs (in this order, so ids increase) for an owner; return their codes.

    Each row may contain URL fields plus "tags": ["Name", ...].
    In tests every row gets the SAME created_at (now() = transaction start), which
    is the hardest case for pagination: only the id tie-breaker orders them.
    """

    async def work(session: AsyncSession) -> list[str]:
        codes = []
        for row in rows:
            fields = dict(row)
            tag_names = fields.pop("tags", [])
            code = fields.pop("short_code", f"list{next(_numbers)}")
            url = URL(
                original_url=fields.pop("original_url", "https://example.com/"),
                short_code=code,
                owner_id=owner_id,
                tags=await TagRepository(session).get_or_create_many(tag_names),
                **fields,
            )
            session.add(url)
            await session.flush()  # one by one: ids increase in this order
            codes.append(code)
        return codes

    return run_db(work)


def page_through(client: TestClient, limit: int, **params: Any) -> tuple[list[str], int]:
    """Follow next_cursor until the end; return (all codes in order, number of pages)."""
    codes: list[str] = []
    cursor = None
    pages = 0
    while True:
        query = {"limit": limit, **params, **({"cursor": cursor} if cursor else {})}
        response = client.get(API, params=query)
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["items"]) <= limit
        codes += [item["short_code"] for item in body["items"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            return codes, pages
        assert pages < 100, "pagination never ended"


def listed(client: TestClient, **params: Any) -> set[str]:
    codes, _ = page_through(client, limit=100, **params)
    return set(codes)


# ---------- keyset pagination ----------


def test_newest_pages_have_no_duplicates_and_no_gaps(
    db_client: TestClient, owner_id: int, run_db: RunDb
) -> None:
    created = seed(run_db, owner_id, *[{} for _ in range(25)])

    codes, pages = page_through(db_client, limit=7)

    assert pages == 4  # 7 + 7 + 7 + 4
    assert len(codes) == len(set(codes)) == 25  # no duplicates, none skipped
    assert codes == list(reversed(created))  # same created_at -> newest id first


def test_newest_orders_by_created_at_then_id(
    db_client: TestClient, owner_id: int, run_db: RunDb
) -> None:
    old, tie_a, tie_b, new = seed(
        run_db,
        owner_id,
        {"created_at": NOW - timedelta(days=2)},
        {"created_at": NOW - timedelta(days=1)},
        {"created_at": NOW - timedelta(days=1)},  # same time as tie_a: id decides
        {"created_at": NOW},
    )

    codes, pages = page_through(db_client, limit=1)  # one per page: every boundary is tested

    assert codes == [new, tie_b, tie_a, old]
    assert pages == 4


def test_most_clicked_pages_with_ties(db_client: TestClient, owner_id: int, run_db: RunDb) -> None:
    clicks = [3, 1, 3, 0, 2, 3, 1, 0, 2, 5]
    created = seed(run_db, owner_id, *[{"click_count": c} for c in clicks])
    expected = [
        code for _, _, code in sorted(zip(clicks, range(len(clicks)), created), reverse=True)
    ]  # click_count DESC, then id DESC

    codes, _ = page_through(db_client, limit=3, sort="most_clicked")

    assert codes == expected
    assert len(set(codes)) == len(clicks)


def test_new_url_created_between_pages_causes_no_repeats(
    db_client: TestClient, owner_id: int, run_db: RunDb
) -> None:
    seed(run_db, owner_id, *[{} for _ in range(6)])
    first = db_client.get(API, params={"limit": 3}).json()

    db_client.post(API, json={"original_url": "https://new.example/"})  # arrives at the front
    second = db_client.get(API, params={"limit": 3, "cursor": first["next_cursor"]}).json()

    first_codes = {item["short_code"] for item in first["items"]}
    second_codes = {item["short_code"] for item in second["items"]}
    assert first_codes.isdisjoint(second_codes)  # OFFSET would have repeated a row here
    assert len(second_codes) == 3


def test_last_page_exactly_full_has_no_next_cursor(
    db_client: TestClient, owner_id: int, run_db: RunDb
) -> None:
    seed(run_db, owner_id, *[{} for _ in range(14)])

    codes, pages = page_through(db_client, limit=7)

    assert (len(codes), pages) == (14, 2)  # no empty third page


def test_empty_list(db_client: TestClient, owner_id: int) -> None:
    assert db_client.get(API).json() == {"items": [], "next_cursor": None}


def test_items_include_owner_and_tags(db_client: TestClient, owner_id: int, run_db: RunDb) -> None:
    seed(run_db, owner_id, {"short_code": "tagged1", "tags": ["Email", "Ads"]})

    item = db_client.get(API).json()["items"][0]

    assert item["owner"] == {"id": owner_id, "username": "me"}
    assert sorted(tag["name"] for tag in item["tags"]) == ["Ads", "Email"]
    assert item["short_url"] == "http://short.test/tagged1"


def test_only_my_not_deleted_urls_are_listed(
    db_client: TestClient, owner_id: int, run_db: RunDb
) -> None:
    mine = seed(run_db, owner_id, {"short_code": "mine1"}, {"short_code": "gone1", "deleted_at": NOW})

    async def other_owner(session: AsyncSession) -> int:
        stranger = User(email="stranger@example.com", username="stranger", hashed_password="h")
        session.add(stranger)
        await session.flush()
        return stranger.id

    seed(run_db, run_db(other_owner), {"short_code": "theirs1"})

    assert listed(db_client) == {mine[0]}


# ---------- filters (each only applied when given) ----------


def test_filter_by_tag_ignores_case_and_never_duplicates(
    db_client: TestClient, owner_id: int, run_db: RunDb
) -> None:
    seed(
        run_db,
        owner_id,
        {"short_code": "both1", "tags": ["Marketing", "Social"]},
        {"short_code": "mkt1", "tags": ["Marketing"]},
        {"short_code": "none1"},
    )

    codes, _ = page_through(db_client, limit=100, tag="marketing")

    assert sorted(codes) == ["both1", "mkt1"]  # each URL once, even with 2 tags


def test_filter_by_is_active(db_client: TestClient, owner_id: int, run_db: RunDb) -> None:
    seed(run_db, owner_id, {"short_code": "on1"}, {"short_code": "off1", "is_active": False})

    assert listed(db_client, is_active=True) == {"on1"}
    assert listed(db_client, is_active=False) == {"off1"}


def test_filter_by_expired_handles_null_expiry(
    db_client: TestClient, owner_id: int, run_db: RunDb
) -> None:
    seed(
        run_db,
        owner_id,
        {"short_code": "never1"},  # expires_at NULL
        {"short_code": "future1", "expires_at": NOW + timedelta(days=1)},
        {"short_code": "past1", "expires_at": NOW - timedelta(days=1)},
    )

    assert listed(db_client, expired=True) == {"past1"}
    assert listed(db_client, expired=False) == {"never1", "future1"}  # the NULL trap avoided


def test_search_in_url_and_title_ignoring_case(
    db_client: TestClient, owner_id: int, run_db: RunDb
) -> None:
    seed(
        run_db,
        owner_id,
        {"short_code": "inurl1", "original_url": "https://www.PYTHON.org/"},
        {"short_code": "intitle1", "title": "Learn Python fast"},
        {"short_code": "other1", "original_url": "https://rust-lang.org/"},
    )

    assert listed(db_client, search="python") == {"inurl1", "intitle1"}


def test_search_treats_percent_and_underscore_literally(
    db_client: TestClient, owner_id: int, run_db: RunDb
) -> None:
    seed(
        run_db,
        owner_id,
        {"short_code": "sale1", "title": "50% off"},
        {"short_code": "plain1", "title": "500 offers"},
    )

    assert listed(db_client, search="50%") == {"sale1"}  # "%" is not a wildcard here
    assert listed(db_client, search="_") == set()


def test_filters_are_combined(db_client: TestClient, owner_id: int, run_db: RunDb) -> None:
    seed(
        run_db,
        owner_id,
        {"short_code": "match1", "tags": ["Ads"], "title": "python promo"},
        {"short_code": "notag1", "title": "python promo"},
        {"short_code": "off1", "tags": ["Ads"], "title": "python promo", "is_active": False},
    )

    assert listed(db_client, tag="Ads", is_active=True, search="python") == {"match1"}


def test_filters_work_with_pagination(db_client: TestClient, owner_id: int, run_db: RunDb) -> None:
    rows = [{"tags": ["Ads"]} if i % 2 == 0 else {} for i in range(10)]
    created = seed(run_db, owner_id, *rows)
    tagged = [code for code, row in zip(created, rows) if row]

    codes, pages = page_through(db_client, limit=2, tag="Ads")

    assert codes == list(reversed(tagged))
    assert pages == 3


# ---------- validation ----------


@pytest.mark.parametrize("limit", [0, 101, -5])
def test_limit_must_be_between_1_and_100(db_client: TestClient, owner_id: int, limit: int) -> None:
    response = db_client.get(API, params={"limit": limit})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_default_limit_is_20(db_client: TestClient, owner_id: int, run_db: RunDb) -> None:
    seed(run_db, owner_id, *[{} for _ in range(21)])

    body = db_client.get(API).json()

    assert len(body["items"]) == 20
    assert body["next_cursor"] is not None


def test_unknown_sort_is_rejected(db_client: TestClient, owner_id: int) -> None:
    assert db_client.get(API, params={"sort": "random"}).status_code == 422


@pytest.mark.parametrize(
    "cursor",
    ["garbage", encode_cursor(URLSort.MOST_CLICKED, (3, 1))],  # wrong sort for the default "newest"
)
def test_bad_cursor_returns_400(db_client: TestClient, owner_id: int, cursor: str) -> None:
    response = db_client.get(API, params={"cursor": cursor})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_cursor"
