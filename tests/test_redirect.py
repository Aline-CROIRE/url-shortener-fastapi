"""GET /{short_code}: redirect only live URLs and hand each visit over to be counted.

In these tests the click is saved at once, in the test transaction (InlineClickTracker
in conftest.py); in production a Celery worker saves it (tests/test_click_task.py).
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import UrlGoneError
from app.models import URL, Click, User
from app.repositories import URLRepository
from app.services.redirect import RedirectService, VisitorInfo

TARGET = "https://www.python.org/about/"
_numbers = count(1)

RunDb = Callable[..., Any]


def seed_url(run_db: RunDb, **fields: Any) -> str:
    """Create an owner + URL in the test transaction; return the short code."""
    n = next(_numbers)
    short_code = fields.pop("short_code", f"live{n}")

    async def work(session: AsyncSession) -> None:
        owner = User(email=f"owner{n}@example.com", username=f"owner{n}", hashed_password="h")
        session.add(URL(original_url=TARGET, short_code=short_code, owner=owner, **fields))

    run_db(work)
    return short_code


def recorded(run_db: RunDb, short_code: str) -> tuple[int, list[Click]]:
    """(click_count, clicks) for one URL, read with a fresh session."""

    async def work(session: AsyncSession) -> tuple[int, list[Click]]:
        url = await session.scalar(select(URL).where(URL.short_code == short_code))
        clicks = await session.scalars(select(Click).where(Click.url_id == url.id))
        return url.click_count, list(clicks)

    return run_db(work)


# ---------- the happy path ----------


def test_redirect_returns_302_and_records_the_click(db_client: TestClient, run_db: RunDb) -> None:
    code = seed_url(run_db)

    response = db_client.get(
        f"/{code}",
        headers={"User-Agent": "Mozilla/5.0 (pytest)", "Referer": "https://twitter.com/"},
    )

    assert response.status_code == 302
    assert response.headers["location"] == TARGET
    click_count, clicks = recorded(run_db, code)
    assert click_count == 1
    assert len(clicks) == 1
    assert clicks[0].user_agent == "Mozilla/5.0 (pytest)"
    assert clicks[0].referrer == "https://twitter.com/"
    assert clicks[0].ip_address is None  # TestClient's host "testclient" is not an IP
    assert clicks[0].clicked_at is not None


def test_every_visit_is_counted(db_client: TestClient, run_db: RunDb) -> None:
    code = seed_url(run_db)

    for _ in range(3):
        assert db_client.get(f"/{code}").status_code == 302

    click_count, clicks = recorded(run_db, code)
    assert click_count == 3
    assert len(clicks) == 3  # the counter and the clicks table always agree


def test_a_visit_does_not_change_updated_at(db_client: TestClient, run_db: RunDb) -> None:
    # A fixed old time: now() would be the same inside the test's one transaction
    last_edit = datetime(2026, 1, 1, tzinfo=UTC)
    code = seed_url(run_db, updated_at=last_edit)

    assert db_client.get(f"/{code}").status_code == 302

    async def updated_at(session: AsyncSession) -> datetime | None:
        return await session.scalar(select(URL.updated_at).where(URL.short_code == code))

    assert run_db(updated_at) == last_edit  # a click is not an edit


def test_custom_alias_redirects(db_client: TestClient, run_db: RunDb) -> None:
    seed_url(run_db, short_code="py-docs", custom_alias="py-docs")

    response = db_client.get("/py-docs")

    assert response.status_code == 302
    assert response.headers["location"] == TARGET


def test_trailing_slash_path_redirects_directly(db_client: TestClient, run_db: RunDb) -> None:
    # "/{short_code}/" is the specification's path: served directly, no 307 first
    code = seed_url(run_db)

    response = db_client.get(f"/{code}/")

    assert response.status_code == 302
    assert response.headers["location"] == TARGET


# ---------- 404 and 410 ----------


def error_code(response: Any) -> str:
    return response.json()["error"]["code"]


def test_unknown_code_returns_404(db_client: TestClient) -> None:
    response = db_client.get("/nothing")

    assert response.status_code == 404
    assert error_code(response) == "url_not_found"


@pytest.mark.parametrize(
    ("fields", "expected_code"),
    [
        ({"expires_at": datetime.now(UTC) - timedelta(days=1)}, "url_expired"),
        ({"is_active": False}, "url_inactive"),
        ({"deleted_at": datetime.now(UTC) - timedelta(hours=1)}, "url_deleted"),
        # Switched off AND expired = what the nightly archive leaves: the real reason is
        # the expiry (Module 8; before, "inactive" was checked first)
        ({"is_active": False, "expires_at": datetime.now(UTC) - timedelta(days=1)}, "url_expired"),
    ],
)
def test_dead_links_return_410_and_record_nothing(
    db_client: TestClient, run_db: RunDb, fields: dict[str, Any], expected_code: str
) -> None:
    code = seed_url(run_db, **fields)

    response = db_client.get(f"/{code}")

    assert response.status_code == 410
    assert error_code(response) == expected_code
    assert recorded(run_db, code) == (0, [])


def test_just_expired_link_is_gone(db_client: TestClient, run_db: RunDb) -> None:
    # The exact boundary, with a frozen clock: expires_at == now -> expired
    moment = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
    code = seed_url(run_db, expires_at=moment)

    async def visit_at_that_moment(session: AsyncSession) -> Any:
        service = RedirectService(session, URLRepository(session))
        return await service.visit(code, VisitorInfo(), now=moment)

    with pytest.raises(UrlGoneError, match="expired"):
        run_db(visit_at_that_moment)
    assert recorded(run_db, code) == (0, [])
