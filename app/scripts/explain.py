"""Show how PostgreSQL runs the app's most important queries.

    python -m app.scripts.seed        # first: enough data to make plans meaningful
    python -m app.scripts.explain

It runs EXPLAIN (ANALYZE, BUFFERS) on the REAL statements built by our
repositories (not hand-copied SQL), with a real popular URL and its owner.
EXPLAIN ANALYZE really executes each query; they are all read-only SELECTs.
"""

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, desc, select
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import get_settings
from app.db.database import create_engine
from app.models import URL
from app.repositories.click import ClickRepository
from app.repositories.url import URLFilters, URLRepository

PAGE_SIZE = 20
NO_DATA_MESSAGE = "No URLs in the database yet. Run first:  python -m app.scripts.seed"


@dataclass(frozen=True)
class ExplainedQuery:
    title: str
    plan: list[str]

    @property
    def indexes_used(self) -> list[str]:
        # "Index Scan using ix_..", "Index Only Scan using ix_..", "Bitmap Index Scan on ix_.."
        text = "\n".join(self.plan)
        return sorted(set(re.findall(r"(?:using|Bitmap Index Scan on) (\w+)", text)))

    @property
    def sequential_scans(self) -> list[str]:
        return sorted(set(re.findall(r"Seq Scan on (\w+)", "\n".join(self.plan))))


async def explain(connection: AsyncConnection, title: str, statement: Select[Any]) -> ExplainedQuery:
    """EXPLAIN the statement exactly as the app sends it: compiled for our driver
    (asyncpg placeholders $1, $2, ...) with the values passed separately.
    """
    compiled = statement.compile(dialect=connection.dialect)
    values = tuple(compiled.params[name] for name in compiled.positiontup or ())
    rows = await connection.exec_driver_sql(f"EXPLAIN (ANALYZE, BUFFERS) {compiled.string}", values)
    return ExplainedQuery(title, [row[0] for row in rows])


async def explain_report(connection: AsyncConnection) -> list[ExplainedQuery]:
    """EXPLAIN ANALYZE the redirect lookup, the URL list and analytics.

    Returns an empty list when there is no URL to explain the queries with.
    """
    url = (
        await connection.execute(
            select(URL.id, URL.short_code, URL.owner_id).order_by(desc(URL.click_count)).limit(1)
        )
    ).first()  # the most clicked URL: the heaviest analytics case
    if url is None:  # empty database: nothing realistic to explain
        return []
    owner_urls = select(URL.created_at, URL.id).where(URL.owner_id == url.owner_id)
    bookmark = (
        await connection.execute(
            owner_urls.order_by(URL.created_at.desc(), URL.id.desc()).offset(PAGE_SIZE * 5).limit(1)
        )
    ).first()  # where page 6 starts (None if the owner has fewer URLs)
    no_filters = URLFilters()
    since = datetime.now(UTC) - timedelta(days=30)

    queries: list[tuple[str, Select[Any]]] = [
        ("Redirect: find a URL by its short code", select(URL).where(URL.short_code == url.short_code)),
        (
            "URL list: first page (newest first)",
            URLRepository.list_owned(url.owner_id, no_filters).limit(PAGE_SIZE + 1),
        ),
        (
            "URL list: page 6 with a keyset cursor",
            URLRepository.list_owned(
                url.owner_id, no_filters, after=tuple(bookmark) if bookmark else None
            ).limit(PAGE_SIZE + 1),
        ),
        ("Analytics: summary (count, distinct IPs, min, max)", ClickRepository.summary(url.id)),
        ("Analytics: clicks per country (GROUP BY)", ClickRepository.per_country(url.id)),
        ("Analytics: clicks per UTC day, last 30 days", ClickRepository.per_day(url.id, since)),
        (
            "Search inside ONE owner's URLs (ILIKE '%...%')",
            URLRepository.list_owned(url.owner_id, URLFilters(search="article 9")).limit(PAGE_SIZE + 1),
        ),
        (
            "Search across ALL URLs (no owner filter) - the honest bad case",
            select(URL.id).where(URL.original_url.ilike("%article 9%")).limit(PAGE_SIZE + 1),
        ),
    ]
    return [await explain(connection, title, statement) for title, statement in queries]


def print_report(report: list[ExplainedQuery]) -> None:
    if not report:
        print(NO_DATA_MESSAGE)
        return
    for item in report:
        print(f"\n{'=' * 100}\n{item.title}\n{'-' * 100}")
        for line in item.plan:
            print(line)
        used = ", ".join(item.indexes_used) or "none"
        seq = ", ".join(item.sequential_scans)
        print(f"--> indexes used: {used}" + (f"   |   SEQ SCAN on: {seq}" if seq else ""))


async def run() -> None:
    engine = create_engine(get_settings().database_url)
    try:
        async with engine.connect() as connection:
            print_report(await explain_report(connection))
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
