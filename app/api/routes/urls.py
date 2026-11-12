from typing import Annotated

from fastapi import APIRouter, Body, status

from app.api.dependencies import UrlServiceDep
from app.api.params import ShortCodePath
from app.api.responses import INTERNAL_ERROR, NOT_FOUND, STORAGE_UNAVAILABLE, VALIDATION_ERROR
from app.schemas.url import LegacyURLCreate, LegacyURLRead
from app.services.url_shortener import UrlShortenerService
from app.storage.base import UrlRecord

router = APIRouter(prefix="/urls", tags=["URLs"])

CREATE_EXAMPLES = {
    "valid": {
        "summary": "Valid URL",
        "value": {"original_url": "https://www.python.org/about/"},
    },
    "invalid": {
        "summary": "Invalid URL (422)",
        "value": {"original_url": "not-a-url"},
    },
}


def to_url_read(record: UrlRecord, service: UrlShortenerService) -> LegacyURLRead:
    return LegacyURLRead(
        short_code=record.short_code,
        original_url=record.original_url,
        short_url=service.build_short_url(record.short_code),
        created_at=record.created_at,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a short URL",
    response_description="Created",
    responses={**VALIDATION_ERROR, **INTERNAL_ERROR, **STORAGE_UNAVAILABLE},
)
async def create_short_url(
    payload: Annotated[LegacyURLCreate, Body(openapi_examples=CREATE_EXAMPLES)],
    service: UrlServiceDep,
) -> LegacyURLRead:
    record = await service.create_short_url(str(payload.original_url))
    return to_url_read(record, service)


@router.get(
    "/{short_code}",
    summary="Get short URL details",
    description="Shows where a short code points, without redirecting.",
    response_description="Details",
    responses={**NOT_FOUND, **VALIDATION_ERROR, **INTERNAL_ERROR, **STORAGE_UNAVAILABLE},
)
async def get_short_url(short_code: ShortCodePath, service: UrlServiceDep) -> LegacyURLRead:
    record = await service.get_url(short_code)  # raises UrlNotFoundError -> 404
    return to_url_read(record, service)
