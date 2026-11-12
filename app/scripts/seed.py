"""Fill the database with realistic fake data (development and performance tests).

    python -m app.scripts.seed                          # 50 users, 10,000 URLs, 200,000 clicks
    python -m app.scripts.seed --urls 500 --clicks 5000
    python -m app.scripts.seed --reset                  # remove earlier seed data first

Everything is inserted with bulk insert() statements inside ONE transaction:
either all rows are saved, or none. Seeded users have an "@seed.local" email, so
--reset removes exactly them (CASCADE removes their URLs, clicks and tag links).
"""

import argparse
import asyncio
import random
import string
import sys
import time
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, exists, insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import get_settings
from app.db.database import create_engine
from app.models import URL, Click, Tag, User, UserTier, url_tags

SEED_EMAIL_DOMAIN = "seed.local"
BATCH_SIZE = 5_000  # rows per INSERT statement
CODE_ALPHABET = string.ascii_letters + string.digits

# (value, weight): weights make the data look real (most clicks from a few places)
COUNTRIES = [("RW", 30), ("GH", 20), ("NG", 15), ("KE", 10), ("US", 10), ("FR", 5), (None, 10)]
CITIES = {
    "RW": ["Kigali", "Huye", "Musanze"],
    "GH": ["Accra", "Kumasi"],
    "NG": ["Lagos", "Abuja"],
    "KE": ["Nairobi", "Mombasa"],
    "US": ["New York", "Paris"],
    "FR": ["Paris", "Lyon"],
    None: [None],
}
REFERRERS = [
    ("https://twitter.com/", 30),
    ("https://www.google.com/", 25),
    ("https://www.facebook.com/", 15),
    ("https://www.linkedin.com/", 10),
    (None, 15),  # no Referer header -> "Direct" in analytics
    ("", 5),  # empty header -> also "Direct"
]
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14) Chrome/130.0 Mobile",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) Firefox/131.0",
]


@dataclass(frozen=True)
class SeedCounts:
    users: int = 50
    urls: int = 10_000
    clicks: int = 200_000


@dataclass(frozen=True)
class SeedReport:
    users: int
    urls: int
    clicks: int
    url_tag_links: int


def batches(rows: Sequence[dict[str, Any]], size: int = BATCH_SIZE) -> Iterator[Sequence[dict[str, Any]]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def weighted(rng: random.Random, choices: list[tuple[Any, int]]) -> Any:
    values, weights = zip(*choices)
    return rng.choices(values, weights=weights)[0]


def random_moment(rng: random.Random, start: datetime, end: datetime) -> datetime:
    return start + (end - start) * rng.random()


async def reset_seeded_data(connection: AsyncConnection) -> int:
    """Delete seeded users; ON DELETE CASCADE removes their URLs, clicks and tag links."""
    result = await connection.execute(
        delete(User).where(User.email.like(f"%@{SEED_EMAIL_DOMAIN}"))
    )
    return result.rowcount


async def seed_database(
    connection: AsyncConnection,
    counts: SeedCounts,
    rng: random.Random,
    now: datetime | None = None,
) -> SeedReport:
    """Insert users, URLs (+ tags) and clicks with bulk insert() statements.

    Uses the caller's connection and transaction (it never commits), so the CLI
    commits once at the end and tests can roll everything back.
    Same rng seed -> same data.
    """
    now = now or datetime.now(UTC)
    already_seeded = await connection.scalar(
        select(exists().where(User.email.like(f"%@{SEED_EMAIL_DOMAIN}")))
    )
    if already_seeded:
        raise RuntimeError("Seed data already exists. Run again with --reset to replace it.")

    # ---- users (RETURNING gives back the new ids, in the same order) ----
    user_rows = [
        {
            "email": f"user{i}@{SEED_EMAIL_DOMAIN}",
            "username": f"seed_user{i}",
            "hashed_password": "!",  # unusable: nobody can log in as a seed user
            "tier": UserTier.PREMIUM if i % 10 == 0 else UserTier.FREE,
            "is_premium": i % 10 == 0,
        }
        for i in range(counts.users)
    ]
    user_ids = list(
        await connection.scalars(
            insert(User).returning(User.id, sort_by_parameter_order=True), user_rows
        )
    )

    # ---- decide which URL receives each click first, so click_count is exact ----
    # Pareto weights: a few URLs are very popular, most get few clicks (like real life)
    popularity = [rng.paretovariate(1.2) for _ in range(counts.urls)]
    click_targets = rng.choices(range(counts.urls), weights=popularity, k=counts.clicks)
    clicks_per_url = Counter(click_targets)

    # ---- URLs ----
    existing_codes = set(await connection.scalars(select(URL.short_code)))
    url_rows = []
    for i in range(counts.urls):
        code = "".join(rng.choices(CODE_ALPHABET, k=7))
        while code in existing_codes:
            code = "".join(rng.choices(CODE_ALPHABET, k=7))
        existing_codes.add(code)
        created_at = random_moment(rng, now - timedelta(days=365), now)
        roll = rng.random()
        url_rows.append(
            {
                "original_url": f"https://example.com/articles/{i}?utm_source=seed",
                "short_code": code,
                "owner_id": rng.choice(user_ids),
                "title": f"Seed article {i}" if rng.random() < 0.6 else None,
                "is_active": roll >= 0.05,  # 5% switched off
                "expires_at": (
                    random_moment(rng, now - timedelta(days=60), now + timedelta(days=60))
                    if rng.random() < 0.2  # 20% have an expiry (some already passed)
                    else None
                ),
                "deleted_at": now if 0.05 <= roll < 0.08 else None,  # 3% soft-deleted
                "click_count": clicks_per_url[i],
                "created_at": created_at,
                "updated_at": created_at,
            }
        )
    url_ids: list[int] = []
    for chunk in batches(url_rows):
        url_ids += await connection.scalars(
            insert(URL).returning(URL.id, sort_by_parameter_order=True), chunk
        )

    # ---- tags: 0-3 of the existing tags per URL (one row per link) ----
    tag_ids = list(await connection.scalars(select(Tag.id)))
    link_rows = [
        {"url_id": url_id, "tag_id": tag_id}
        for url_id in url_ids
        for tag_id in rng.sample(tag_ids, k=min(len(tag_ids), rng.choice([0, 0, 1, 1, 2, 3])))
    ]
    for chunk in batches(link_rows):
        await connection.execute(insert(url_tags), chunk)

    # ---- clicks: never before the URL existed, within the last 90 days ----
    visitor_ips = [f"41.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}" for _ in range(5_000)]
    click_rows = []
    for url_index in click_targets:
        country = weighted(rng, COUNTRIES)
        first_possible = max(url_rows[url_index]["created_at"], now - timedelta(days=90))
        click_rows.append(
            {
                "url_id": url_ids[url_index],
                "clicked_at": random_moment(rng, first_possible, now),
                "ip_address": rng.choice(visitor_ips) if rng.random() < 0.97 else None,
                "user_agent": rng.choice(USER_AGENTS),
                "referrer": weighted(rng, REFERRERS),
                "country": country,
                "city": rng.choice(CITIES[country]),
            }
        )
    for chunk in batches(click_rows):
        await connection.execute(insert(Click), chunk)

    return SeedReport(
        users=len(user_ids), urls=len(url_ids), clicks=len(click_rows), url_tag_links=len(link_rows)
    )


async def run(counts: SeedCounts, seed: int, reset: bool) -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    started = time.perf_counter()
    try:
        async with engine.begin() as connection:  # ONE transaction: COMMIT at the end
            if reset:
                removed = await reset_seeded_data(connection)
                print(f"Removed {removed} seeded users (and, by CASCADE, their URLs and clicks)")
            report = await seed_database(connection, counts, random.Random(seed))
        async with engine.begin() as connection:
            # Refresh the planner's statistics so EXPLAIN sees the new table sizes
            await connection.execute(text("ANALYZE users, urls, clicks, tags, url_tags"))
    finally:
        await engine.dispose()
    seconds = time.perf_counter() - started
    print(
        f"Seeded {report.users} users, {report.urls} URLs, {report.url_tag_links} tag links "
        f"and {report.clicks} clicks in {seconds:.1f}s (one transaction)."
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Fill the database with fake data.")
    parser.add_argument("--users", type=int, default=SeedCounts.users)
    parser.add_argument("--urls", type=int, default=SeedCounts.urls)
    parser.add_argument("--clicks", type=int, default=SeedCounts.clicks)
    parser.add_argument("--seed", type=int, default=42, help="same seed -> same data")
    parser.add_argument("--reset", action="store_true", help="remove earlier seed data first")
    args = parser.parse_args(argv)

    if get_settings().environment == "production":
        sys.exit("Refusing to seed: ENVIRONMENT is 'production'.")
    if min(args.users, args.urls) < 1 or args.clicks < 0:
        sys.exit("--users and --urls must be at least 1, --clicks at least 0.")

    asyncio.run(run(SeedCounts(args.users, args.urls, args.clicks), args.seed, args.reset))


if __name__ == "__main__":
    main()
