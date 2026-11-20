"""The analytics service: click statistics for one short URL."""

from datetime import datetime, time, timedelta

from app.core.exceptions import ForbiddenError, UrlNotFoundError
from app.repositories import ClickRepository, URLRepository
from app.repositories.url import utc_now
from app.schemas.analytics import (
    CityCount,
    CountryCount,
    DailyCount,
    ReferrerCount,
    URLAnalytics,
)


def window_start(now: datetime, days: int) -> datetime:
    """Return midnight (UTC) at the start of the window.

    days=1 -> today, days=7 -> 6 days ago.
    """
    today_midnight = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
    return today_midnight - timedelta(days=days - 1)


class AnalyticsService:
    """Builds the analytics report of one URL.

    Postgres does every count (see ClickRepository). This class only checks the
    owner and turns the few result rows into schemas: it never counts in Python.
    Read only, so no transaction is opened.
    """

    def __init__(self, urls: URLRepository, clicks: ClickRepository) -> None:
        """Keep the repositories the service reads from."""
        self._urls = urls
        self._clicks = clicks

    async def for_url(
        self,
        short_code: str,
        actor_id: int,
        days: int,
        now: datetime | None = None,
        *,
        is_admin: bool = False,
    ) -> URLAnalytics:
        """Build the analytics report of one URL, for its owner or an Admin.

        The tier (Premium or Admin) is checked before this, by the route's
        dependency; this method checks the OWNER.

        Args:
            short_code: The short code of the URL.
            actor_id: The id of the user asking.
            days: How many days the clicks_per_day list covers (today included).
            now: The current time; tests pass a fixed value.
            is_admin: True lets the caller see any URL's analytics.

        Raises:
            UrlNotFoundError: If the URL does not exist or is deleted.
            ForbiddenError: If it is someone else's and the caller is not an Admin.
        """
        found = await self._urls.id_and_owner_id(short_code)
        if found is None:  # unknown or deleted
            raise UrlNotFoundError(short_code)
        url_id, owner_id = found
        if owner_id != actor_id and not is_admin:
            raise ForbiddenError()

        now = now or utc_now()
        clicks = self._clicks
        summary = await clicks.fetch_one_row(ClickRepository.summary(url_id))
        countries = await clicks.fetch_rows(ClickRepository.per_country(url_id))
        cities = await clicks.fetch_rows(ClickRepository.per_city(url_id))
        per_day = await clicks.fetch_rows(ClickRepository.per_day(url_id, window_start(now, days)))
        referrers = await clicks.fetch_rows(ClickRepository.top_referrers(url_id))

        # from_attributes: each labelled row (row.country, row.clicks) -> schema
        return URLAnalytics(
            short_code=short_code,
            days=days,
            total_clicks=summary.total_clicks,
            unique_visitors=summary.unique_visitors,
            first_click_at=summary.first_click_at,
            last_click_at=summary.last_click_at,
            clicks_per_country=[CountryCount.model_validate(row) for row in countries],
            clicks_per_city=[CityCount.model_validate(row) for row in cities],
            clicks_per_day=[DailyCount.model_validate(row) for row in per_day],
            top_referrers=[ReferrerCount.model_validate(row) for row in referrers],
        )
