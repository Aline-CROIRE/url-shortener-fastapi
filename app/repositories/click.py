"""Database access for clicks, including the analytics queries."""

from datetime import datetime
from typing import Any

from sqlalchemy import Date, Row, Select, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Click

UNKNOWN = "Unknown"  # shown when no country/city was recorded
DIRECT = "Direct"  # shown when the visitor came without a referrer
TOP_REFERRERS = 10

# ---------- reusable labelled SQL expressions ----------
# .label() names each result column, so a schema can read row.country, row.clicks

# case(): SQL's if/else. A NULL country goes on the "Unknown" pile instead of
# being a missing key.
country_or_unknown = case((Click.country.is_(None), UNKNOWN), else_=Click.country)
city_or_unknown = case((Click.city.is_(None), UNKNOWN), else_=Click.city)
# nullif(referrer, '') turns an empty header into NULL; coalesce() then picks "Direct"
referrer_or_direct = func.coalesce(func.nullif(Click.referrer, ""), DIRECT)
# The UTC calendar day of a click. timezone('UTC', ...) first, so the day does not
# depend on the database session's time zone (23:30 in New York is already the
# next day in UTC).
utc_day = cast(func.date_trunc("day", func.timezone("UTC", Click.clicked_at)), Date)
click_count = func.count().label("clicks")


class ClickRepository:
    """All database access for clicks, including analytics.

    Analytics rule: Postgres does ALL the counting (COUNT, GROUP BY, MIN, MAX).
    Python never loops over clicks; it only receives a few summary rows.
    Every query filters on url_id, so it can use the (url_id, clicked_at) index.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Keep the session used for every query."""
        self._session = session

    @staticmethod
    def for_url(url_id: int) -> Select[tuple[Click]]:
        """Clicks of one URL, newest first (uses the (url_id, clicked_at) index)."""
        return (
            select(Click)
            .where(Click.url_id == url_id)
            .order_by(Click.clicked_at.desc(), Click.id.desc())
        )

    # ---------- analytics statements ----------

    @staticmethod
    def summary(url_id: int) -> Select[Any]:
        """ONE row: total clicks, unique visitors, first and last click."""
        return select(
            func.count().label("total_clicks"),
            # COUNT(DISTINCT ip): different IPs only; NULL IPs are not counted
            func.count(Click.ip_address.distinct()).label("unique_visitors"),
            func.min(Click.clicked_at).label("first_click_at"),
            func.max(Click.clicked_at).label("last_click_at"),
        ).where(Click.url_id == url_id)

    @staticmethod
    def per_country(url_id: int) -> Select[Any]:
        """Return a statement counting clicks per country, most clicks first."""
        country = country_or_unknown.label("country")
        return (
            select(country, click_count)
            .where(Click.url_id == url_id)
            .group_by(country)  # one "pile" per country
            .order_by(click_count.desc(), country)  # stable order: clicks, then name
        )

    @staticmethod
    def per_city(url_id: int) -> Select[Any]:
        """Return a statement counting clicks per city and country, most clicks first."""
        # Grouped by city AND country: Paris (FR) and Paris (US) are different places
        city = city_or_unknown.label("city")
        country = country_or_unknown.label("country")
        return (
            select(city, country, click_count)
            .where(Click.url_id == url_id)
            .group_by(city, country)
            .order_by(click_count.desc(), city, country)
        )

    @staticmethod
    def per_day(url_id: int, since: datetime) -> Select[Any]:
        """Time series: clicks per UTC day from `since`, oldest day first.

        Days without clicks are not listed (a chart fills the gaps; Postgres could
        also do it with generate_series()).
        """
        day = utc_day.label("day")
        return (
            select(day, click_count)
            .where(Click.url_id == url_id, Click.clicked_at >= since)
            .group_by(day)
            .order_by(day)
        )

    @staticmethod
    def top_referrers(url_id: int, limit: int = TOP_REFERRERS) -> Select[Any]:
        """Return a statement for the sites visitors came from, most clicks first.

        At most `limit` rows; no referrer is counted as "Direct".
        """
        referrer = referrer_or_direct.label("referrer")
        return (
            select(referrer, click_count)
            .where(Click.url_id == url_id)
            .group_by(referrer)
            .order_by(click_count.desc(), referrer)
            .limit(limit)
        )

    # ---------- running statements ----------

    async def fetch_all(self, statement: Select[tuple[Click]]) -> list[Click]:
        """Run the statement and return every click it finds."""
        return list(await self._session.scalars(statement))

    async def fetch_rows(self, statement: Select[Any]) -> list[Row[Any]]:
        """Run the statement and return rows with named columns (row.country, row.clicks)."""
        return list(await self._session.execute(statement))

    async def fetch_one_row(self, statement: Select[Any]) -> Row[Any]:
        """Run the statement and return its single result row.

        Raises:
            sqlalchemy.exc.NoResultFound: if there is no row.
            sqlalchemy.exc.MultipleResultsFound: if there is more than one row.
        """
        return (await self._session.execute(statement)).one()

    async def add(self, click: Click) -> Click:
        """Add the click and flush so its id is filled in (no commit)."""
        self._session.add(click)
        await self._session.flush()
        return click
