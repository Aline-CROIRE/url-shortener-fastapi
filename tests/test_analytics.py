"""GET /api/v1/analytics/{short_code}: every number is computed by PostgreSQL.

The clicks below are chosen by hand so each expected number can be checked on paper.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import URL, Click, User
from app.repositories import ClickRepository

API = "/api/v1/analytics"
RunDb = Callable[..., Any]

TODAY = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)  # midnight UTC
D0 = TODAY - timedelta(days=3)  # three full days ago, so all test clicks are in the past
NEW_YORK = timezone(timedelta(hours=-5))  # UTC-5 (winter time)

CLICKS = [
    # (clicked_at, country, city, ip, referrer)
    (D0 + timedelta(hours=1), "RW", "Kigali", "41.0.0.1", "https://twitter.com/"),
    (D0 + timedelta(hours=2), "RW", "Kigali", "41.0.0.1", "https://twitter.com/"),  # same IP
    # 04:30 UTC on D0 = 23:30 the evening BEFORE in New York -> must count on D0 (UTC)
    ((D0 + timedelta(hours=4, minutes=30)).astimezone(NEW_YORK), "RW", "Huye", "41.0.0.2", None),
    (D0 + timedelta(days=1, hours=3), "GH", "Accra", "41.0.0.3", ""),  # empty -> Direct
    (D0 + timedelta(days=1, hours=4), "GH", "Accra", None, "https://google.com/"),  # no IP
    (D0 + timedelta(days=2, hours=1), None, None, "41.0.0.4", "https://twitter.com/"),
    (D0 + timedelta(days=2, hours=2), "US", "Paris", "8.8.8.8", "https://google.com/"),
    (D0 + timedelta(days=2, hours=3), "FR", "Paris", "2001:db8::1", "https://google.com/"),
    (TODAY - timedelta(days=100), "RW", "Kigali", "41.0.0.5", "https://twitter.com/"),  # old
]


def as_dict(rows: list[dict[str, Any]], *keys: str) -> dict[Any, int]:
    """[{"country": "RW", "clicks": 4}, ...] -> {"RW": 4, ...} (order checked separately)."""
    return {
        tuple(row[k] for k in keys) if len(keys) > 1 else row[keys[0]]: row["clicks"]
        for row in rows
    }


def assert_most_clicked_first(rows: list[dict[str, Any]]) -> None:
    counts = [row["clicks"] for row in rows]
    assert counts == sorted(counts, reverse=True)


@pytest.fixture
def stats_code(run_db: RunDb, owner_id: int) -> str:
    async def work(session: AsyncSession) -> str:
        url = URL(original_url="https://www.python.org/", short_code="stats1", owner_id=owner_id)
        other = URL(original_url="https://other.example/", short_code="other1", owner_id=owner_id)
        session.add_all([url, other])
        await session.flush()
        for clicked_at, country, city, ip, referrer in CLICKS:
            session.add(
                Click(
                    url_id=url.id,
                    clicked_at=clicked_at,
                    country=country,
                    city=city,
                    ip_address=ip,
                    referrer=referrer,
                )
            )
        for _ in range(2):  # clicks of ANOTHER url: must never be counted
            session.add(Click(url_id=other.id, clicked_at=D0, country="RW", ip_address="9.9.9.9"))
        return url.short_code

    return run_db(work)


@pytest.fixture
def report(db_client: TestClient, stats_code: str) -> dict[str, Any]:
    response = db_client.get(f"{API}/{stats_code}")
    assert response.status_code == 200, response.text
    return response.json()


# ---------- the numbers ----------


def test_summary(report: dict[str, Any]) -> None:
    assert report["short_code"] == "stats1"
    assert report["days"] == 30
    assert report["total_clicks"] == 9  # the other URL's 2 clicks are not included
    # 7 different IPs: 41.0.0.1 (twice) counts once, the click without IP not at all
    assert report["unique_visitors"] == 7
    assert datetime.fromisoformat(report["first_click_at"]) == CLICKS[8][0]  # the old one
    assert datetime.fromisoformat(report["last_click_at"]) == CLICKS[7][0]


def test_clicks_per_country_with_unknown(report: dict[str, Any]) -> None:
    rows = report["clicks_per_country"]

    assert as_dict(rows, "country") == {"RW": 4, "GH": 2, "US": 1, "FR": 1, "Unknown": 1}
    assert rows[0] == {"country": "RW", "clicks": 4}
    assert_most_clicked_first(rows)


def test_clicks_per_city_keeps_same_names_in_different_countries_apart(
    report: dict[str, Any],
) -> None:
    rows = report["clicks_per_city"]

    assert as_dict(rows, "city", "country") == {
        ("Kigali", "RW"): 3,
        ("Accra", "GH"): 2,
        ("Huye", "RW"): 1,
        ("Paris", "US"): 1,
        ("Paris", "FR"): 1,
        ("Unknown", "Unknown"): 1,
    }
    assert_most_clicked_first(rows)


def test_clicks_per_day_in_utc_oldest_first(report: dict[str, Any]) -> None:
    # The New York 23:30 click counts on D0 (its UTC day); the 100-day-old click is
    # outside the 30-day window
    assert report["clicks_per_day"] == [
        {"day": D0.date().isoformat(), "clicks": 3},
        {"day": (D0.date() + timedelta(days=1)).isoformat(), "clicks": 2},
        {"day": (D0.date() + timedelta(days=2)).isoformat(), "clicks": 3},
    ]


def test_top_referrers_with_direct(report: dict[str, Any]) -> None:
    assert report["top_referrers"] == [
        {"referrer": "https://twitter.com/", "clicks": 4},
        {"referrer": "https://google.com/", "clicks": 3},
        {"referrer": "Direct", "clicks": 2},  # NULL and "" together
    ]


def test_days_window_can_include_older_clicks(db_client: TestClient, stats_code: str) -> None:
    report = db_client.get(f"{API}/{stats_code}", params={"days": 365}).json()

    days = {row["day"]: row["clicks"] for row in report["clicks_per_day"]}
    assert days[(TODAY - timedelta(days=100)).date().isoformat()] == 1
    assert sum(days.values()) == 9


def test_days_window_of_one_means_today_only(db_client: TestClient, stats_code: str) -> None:
    report = db_client.get(f"{API}/{stats_code}", params={"days": 1}).json()

    assert report["clicks_per_day"] == []  # all test clicks are before today
    assert report["total_clicks"] == 9  # the window only limits the time series


def test_day_does_not_depend_on_the_session_time_zone(run_db: RunDb, stats_code: str) -> None:
    """Even with the database session set to New York time, days are UTC days."""

    async def per_day_in_new_york_session(session: AsyncSession) -> list[tuple[date, int]]:
        await session.execute(text("SET LOCAL TIME ZONE 'America/New_York'"))
        url_id = await session.scalar(text("SELECT id FROM urls WHERE short_code = 'stats1'"))
        rows = await ClickRepository(session).fetch_rows(ClickRepository.per_day(url_id, D0))
        return [(row.day, row.clicks) for row in rows]

    assert run_db(per_day_in_new_york_session)[0] == (D0.date(), 3)


# ---------- edge cases ----------


def test_url_without_clicks(db_client: TestClient, run_db: RunDb, owner_id: int) -> None:
    async def work(session: AsyncSession) -> None:
        session.add(
            URL(original_url="https://quiet.example/", short_code="quiet1", owner_id=owner_id)
        )

    run_db(work)

    report = db_client.get(f"{API}/quiet1").json()

    assert report == {
        "short_code": "quiet1",
        "days": 30,
        "total_clicks": 0,
        "unique_visitors": 0,
        "first_click_at": None,
        "last_click_at": None,
        "clicks_per_country": [],
        "clicks_per_city": [],
        "clicks_per_day": [],
        "top_referrers": [],
    }


def test_unknown_or_someone_elses_url_returns_404(
    db_client: TestClient, run_db: RunDb, owner_id: int
) -> None:
    async def work(session: AsyncSession) -> None:
        stranger = User(email="stranger@example.com", username="stranger", hashed_password="h")
        session.add(URL(original_url="https://x.example/", short_code="theirs1", owner=stranger))

    run_db(work)

    assert db_client.get(f"{API}/nothing").status_code == 404
    assert db_client.get(f"{API}/theirs1").status_code == 404


def test_deleted_url_returns_404(db_client: TestClient, stats_code: str) -> None:
    db_client.delete(f"/api/v1/urls/{stats_code}")

    assert db_client.get(f"{API}/{stats_code}").status_code == 404


@pytest.mark.parametrize("days", [0, 366])
def test_days_must_be_between_1_and_365(db_client: TestClient, stats_code: str, days: int) -> None:
    response = db_client.get(f"{API}/{stats_code}", params={"days": days})

    assert response.status_code == 422
