from datetime import datetime, time, timedelta

from app.core.exceptions import UrlNotFoundError
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
    """Midnight (UTC) at the start of the window: days=1 -> today, days=7 -> 6 days ago."""
    today_midnight = datetime.combine(now.date(), time.min, tzinfo=now.tzinfo)
    return today_midnight - timedelta(days=days - 1)


class AnalyticsService:
    """Builds the analytics report of one URL.

    Postgres does every count (see ClickRepository). This class only checks the
    owner and turns the few result rows into schemas: it never counts in Python.
    Read only, so no transaction is opened.
    """

    def __init__(self, urls: URLRepository, clicks: ClickRepository) -> None:
        self._urls = urls
        self._clicks = clicks

    async def for_url(
        self, short_code: str, owner_id: int, days: int, now: datetime | None = None
    ) -> URLAnalytics:
        url_id = await self._urls.owned_url_id(short_code, owner_id)
        if url_id is None:  # unknown, someone else's, or deleted
            raise UrlNotFoundError(short_code)

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
