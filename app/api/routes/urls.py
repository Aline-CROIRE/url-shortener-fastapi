from fastapi import APIRouter, HTTPException, status

from app.api.dependencies import UrlServiceDep
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
)
async def create_short_url(payload: URLCreate, service: UrlServiceDep) -> URLRead:
    record = await service.create_short_url(str(payload.original_url))
    return to_url_read(record, service)


@router.get(
    "/{short_code}",
    summary="Get short URL info",
    description="Returns the details of a short code without redirecting.",
    responses={status.HTTP_404_NOT_FOUND: {"description": "Short code does not exist"}},
)
async def get_short_url(short_code: str, service: UrlServiceDep) -> URLRead:
    record = await service.get_url(short_code)

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Short URL not found",
        )

    return to_url_read(record, service)
