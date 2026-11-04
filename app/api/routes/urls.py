from typing import Annotated

from fastapi import APIRouter, Body, status

from app.api.dependencies import UrlServiceDep
from app.api.params import ShortCodePath
from app.api.responses import INTERNAL_ERROR, NOT_FOUND, STORAGE_UNAVAILABLE, VALIDATION_ERROR
from app.schemas.url import URLCreate, URLRead
from app.services.url_shortener import UrlShortenerService
from app.storage.base import UrlRecord

router = APIRouter(prefix="/urls", tags=["URLs"])

CREATE_EXAMPLES = {
    "simple": {
        "summary": "A simple URL",
        "value": {"original_url": "https://www.python.org/about/"},
    },
    "long_with_query": {
        "summary": "A long URL with a query string",
        "value": {"original_url": "https://www.google.com/search?q=fastapi+url+shortener&hl=en"},
    },
    "invalid": {
        "summary": "Invalid URL (returns 422)",
        "value": {"original_url": "not-a-url"},
    },
}


def to_url_read(record: UrlRecord, service: UrlShortenerService) -> URLRead:
    return URLRead(
        short_code=record.short_code,
        original_url=record.original_url,
        short_url=service.build_short_url(record.short_code),
        created_at=record.created_at,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a short URL",
    description=(
        "Validates the long URL (must be `http://` or `https://`), generates a unique "
        "7-character code and stores it.\n\n"
        "Returns **201 Created** with the code and the full `short_url` to share."
    ),
    response_description="The short URL was created",
    responses={**VALIDATION_ERROR, **INTERNAL_ERROR, **STORAGE_UNAVAILABLE},
)
async def create_short_url(
    payload: Annotated[URLCreate, Body(openapi_examples=CREATE_EXAMPLES)],
    service: UrlServiceDep,
) -> URLRead:
    record = await service.create_short_url(str(payload.original_url))
    return to_url_read(record, service)


@router.get(
    "/{short_code}",
    summary="Get short URL info",
    description=(
        "Returns the details of a short code **without redirecting**. "
        "Useful to check where a short link goes before opening it."
    ),
    response_description="The short URL details",
    responses={**NOT_FOUND, **VALIDATION_ERROR, **INTERNAL_ERROR, **STORAGE_UNAVAILABLE},
)
async def get_short_url(short_code: ShortCodePath, service: UrlServiceDep) -> URLRead:
    record = await service.get_url(short_code)  # raises UrlNotFoundError -> 404
    return to_url_read(record, service)
