"""Endpoint for the click analytics of one short URL."""

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import AnalyticsServiceDep
from app.api.params import ShortCodePath
from app.api.permissions import PremiumUser, is_admin
from app.api.responses import (
    FORBIDDEN,
    INTERNAL_ERROR,
    NOT_FOUND,
    UNAUTHORIZED,
    VALIDATION_ERROR,
)
from app.schemas.analytics import URLAnalytics

router = APIRouter(prefix="/analytics", tags=["Analytics"])

MAX_DAYS = 365


@router.get("/{short_code}", include_in_schema=False)  # alias without the slash
@router.get(
    "/{short_code}/",
    summary="Click analytics of one of your short URLs (Premium)",
    description=(
        "Totals, unique visitors (estimated by IP), first/last click, and clicks per "
        "country, city, UTC day (last `days` days) and top referrers. "
        "All numbers are computed by PostgreSQL. **Premium and Admin only**, and only for "
        "your own URLs (an Admin may see any). "
        "Locations: IP Geolocation by [DB-IP](https://db-ip.com) (CC BY 4.0); clicks from "
        'private or unknown addresses count as "Unknown".'
    ),
    responses={**UNAUTHORIZED, **FORBIDDEN, **NOT_FOUND, **VALIDATION_ERROR, **INTERNAL_ERROR},
)
async def get_url_analytics(
    short_code: ShortCodePath,
    service: AnalyticsServiceDep,
    user: PremiumUser,  # the TIER check runs first: a Free user learns nothing about codes
    days: Annotated[
        int, Query(ge=1, le=MAX_DAYS, description="How many days clicks_per_day covers")
    ] = 30,
) -> URLAnalytics:
    """Return the click analytics of one of your short URLs."""
    return await service.for_url(short_code, user.id, days, is_admin=is_admin(user))
