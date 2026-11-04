from fastapi import APIRouter, status

from app.api.dependencies import UrlServiceDep
from app.api.responses import NOT_FOUND, STORAGE_UNAVAILABLE, VALIDATION_ERROR
from app.schemas.url import URLCreate, URLRead
from app.services.url_shortener import UrlShortenerService
from app.storage.base import UrlRecord

router = APIRouter(prefix="/urls", tags=["URLs"])


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
    description="Takes a long URL and returns a new 7-character short code and short link.",
    responses={**VALIDATION_ERROR, **STORAGE_UNAVAILABLE},
)
async def create_short_url(payload: URLCreate, service: UrlServiceDep) -> URLRead:
    record = await service.create_short_url(str(payload.original_url))
    return to_url_read(record, service)


@router.get(
    "/{short_code}",
    summary="Get short URL info",
    description="Returns the details of a short code without redirecting.",
    responses={**NOT_FOUND, **VALIDATION_ERROR, **STORAGE_UNAVAILABLE},
)
async def get_short_url(short_code: str, service: UrlServiceDep) -> URLRead:
    record = await service.get_url(short_code)  # raises UrlNotFoundError -> 404
    return to_url_read(record, service)
