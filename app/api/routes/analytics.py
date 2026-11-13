from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import AnalyticsServiceDep
from app.api.owner import CurrentOwnerId
from app.api.params import ShortCodePath
from app.api.responses import INTERNAL_ERROR, NOT_FOUND, VALIDATION_ERROR
from app.schemas.analytics import URLAnalytics

router = APIRouter(prefix="/analytics", tags=["Analytics"])

MAX_DAYS = 365


@router.get("/{short_code}", include_in_schema=False)  # alias without the slash
@router.get(
    "/{short_code}/",
    summary="Click analytics of one of your short URLs",
    description=(
        "Totals, unique visitors (estimated by IP), first/last click, and clicks per "
        "country, city, UTC day (last `days` days) and top referrers. "
        "All numbers are computed by PostgreSQL."
    ),
    responses={**NOT_FOUND, **VALIDATION_ERROR, **INTERNAL_ERROR},
)
async def get_url_analytics(
    short_code: ShortCodePath,
    service: AnalyticsServiceDep,
    owner_id: CurrentOwnerId,
    days: Annotated[
        int, Query(ge=1, le=MAX_DAYS, description="How many days clicks_per_day covers")
    ] = 30,
) -> URLAnalytics:
    # TODO(module-7): premium-only feature once authentication and tiers exist
    return await service.for_url(short_code, owner_id, days)
