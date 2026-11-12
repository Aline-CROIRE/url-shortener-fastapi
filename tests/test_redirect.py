"""GET /{short_code}: redirect only live URLs and record each visit in ONE transaction."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import UrlGoneError
from app.models import URL, Click, User
from app.repositories import ClickRepository, URLRepository
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


def total_clicks(run_db: RunDb) -> int:
    async def work(session: AsyncSession) -> int:
        return await session.scalar(select(func.count()).select_from(Click))

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


def test_custom_alias_redirects(db_client: TestClient, run_db: RunDb) -> None:
    seed_url(run_db, short_code="py-docs", custom_alias="py-docs")

    response = db_client.get("/py-docs")

    assert response.status_code == 302
    assert response.headers["location"] == TARGET


def test_trailing_slash_redirects_to_the_canonical_path(db_client: TestClient, run_db: RunDb) -> None:
    code = seed_url(run_db)

    response = db_client.get(f"/{code}/")

    assert response.status_code == 307  # FastAPI's redirect_slashes
    assert response.headers["location"].endswith(f"/{code}")


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
        ({"is_active": False, "expires_at": datetime.now(UTC) - timedelta(days=1)}, "url_inactive"),
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

    async def visit_at_that_moment(session: AsyncSession) -> str:
        service = RedirectService(session, URLRepository(session), ClickRepository(session))
        return await service.visit(code, VisitorInfo(), now=moment)

    with pytest.raises(UrlGoneError, match="expired"):
        run_db(visit_at_that_moment)
    assert recorded(run_db, code) == (0, [])


# ---------- one transaction: all or nothing ----------


def test_failure_after_the_click_insert_leaves_nothing_half_saved(
    db_client: TestClient, run_db: RunDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = seed_url(run_db)

    async def broken_increment(self: URLRepository, url_id: int) -> None:
        raise RuntimeError("database hiccup during UPDATE")

    # Write 1 (INSERT click) succeeds, then write 2 (UPDATE count) fails
    monkeypatch.setattr(URLRepository, "increment_click_count", broken_increment)

    response = db_client.get(f"/{code}")

    assert response.status_code == 500
    assert recorded(run_db, code) == (0, [])  # the click INSERT was rolled back too


# ---------- Module 5 links (strangler fig fallback) ----------


def test_links_from_the_module_5_endpoint_still_redirect(db_client: TestClient, run_db: RunDb) -> None:
    created = db_client.post("/api/v1/urls", json={"original_url": TARGET}).json()

    response = db_client.get(f"/{created['short_code']}")

    assert response.status_code == 302
    assert response.headers["location"] == TARGET
    assert total_clicks(run_db) == 0  # legacy visits are not recorded
