"""Response shapes for GET /api/v1/analytics/{short_code}/ (built in F2.5).

Each "...Count" schema is filled from one SQL result row whose columns are
named with .label(...), e.g. select(Click.country.label("country"),
func.count().label("clicks")). from_attributes reads row.country, row.clicks.
"""

from datetime import date, datetime

from app.schemas.base import ORMModel


class CountryCount(ORMModel):
    country: str  # ISO code, or "Unknown" when no country was recorded
    clicks: int


class CityCount(ORMModel):
    city: str
    clicks: int


class DailyCount(ORMModel):
    day: date
    clicks: int


class ReferrerCount(ORMModel):
    referrer: str  # "Direct" when the visitor came without a referrer
    clicks: int


class URLAnalytics(ORMModel):
    short_code: str
    total_clicks: int
    unique_visitors: int  # distinct IP addresses
    first_click_at: datetime | None  # None when the link was never clicked
    last_click_at: datetime | None
    clicks_per_country: list[CountryCount]
    clicks_per_city: list[CityCount]
    clicks_per_day: list[DailyCount]
    top_referrers: list[ReferrerCount]
