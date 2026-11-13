"""Endpoints to create, list, get, update and delete short URLs."""

from typing import Annotated

from fastapi import APIRouter, Body, Query, Response, status

from app.api.dependencies import SettingsDep, URLReadServiceDep, URLServiceDep
from app.api.owner import CurrentOwnerId
from app.api.params import ShortCodePath
from app.api.responses import (
    CONFLICT,
    INTERNAL_ERROR,
    INVALID_CURSOR,
    NOT_FOUND,
    VALIDATION_ERROR,
)
from app.core.config import Settings
from app.models import URL
from app.models.tag import TAG_NAME_MAX_LENGTH
from app.repositories.url import MAX_LIMIT, URLFilters, URLSort
from app.schemas.url import URLCreate, URLPage, URLRead, URLUpdate

router = APIRouter(prefix="/urls", tags=["URLs"])

# Every endpoint answers on the specification's path WITH a trailing slash
# ("/api/v1/urls/", documented in Swagger) and on the same path WITHOUT it
# (hidden alias, kept for Module 5 links and existing clients). Neither redirects.

CREATE_EXAMPLES = {
    "minimal": {
        "summary": "Only the long URL",
        "value": {"original_url": "https://www.python.org/about/"},
    },
    "full": {
        "summary": "Alias, expiry and tags",
        "value": {
            "original_url": "https://www.python.org/about/",
            "custom_alias": "py-about",
            "expires_at": "2027-01-31T23:59:59Z",
            "tags": ["Marketing", "Social"],
        },
    },
}
UPDATE_EXAMPLES = {
    "switch_off": {"summary": "Switch the link off", "value": {"is_active": False}},
    "new_tags": {"summary": "Replace the tags", "value": {"tags": ["Email"]}},
    "remove_expiry": {"summary": "Remove the expiry date", "value": {"expires_at": None}},
}


def to_url_read(url: URL, settings: Settings) -> URLRead:
    """Convert a URL model into the URLRead response, with its full short_url."""
    # short_url is built from base_url (a setting), passed as validation context
    return URLRead.model_validate(url, context={"base_url": settings.base_url})


@router.post("", status_code=status.HTTP_201_CREATED, include_in_schema=False)
@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    summary="Create a short URL",
    description="URL and tags are saved in one transaction. A custom alias becomes the short code.",
    responses={**CONFLICT, **VALIDATION_ERROR, **INTERNAL_ERROR},
)
async def create_url(
    payload: Annotated[URLCreate, Body(openapi_examples=CREATE_EXAMPLES)],
    service: URLServiceDep,
    owner_id: CurrentOwnerId,
    settings: SettingsDep,
) -> URLRead:
    """Create a short URL."""
    url = await service.create(payload, owner_id)
    return to_url_read(url, settings)


@router.get("", include_in_schema=False)
@router.get(
    "/",
    summary="List your short URLs",
    description=(
        "Keyset (cursor) pagination: pass `next_cursor` from one page as `cursor` to get "
        "the next. Filters are combined with AND and only applied when given."
    ),
    responses={**INVALID_CURSOR, **VALIDATION_ERROR, **INTERNAL_ERROR},
)
async def list_urls(
    service: URLReadServiceDep,  # read side (replica)
    owner_id: CurrentOwnerId,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT, description="Page size")] = 20,
    cursor: Annotated[str | None, Query(description="next_cursor from the previous page")] = None,
    sort: Annotated[URLSort, Query(description="newest or most_clicked")] = URLSort.NEWEST,
    tag: Annotated[
        str | None,
        Query(min_length=1, max_length=TAG_NAME_MAX_LENGTH, description="Tag name (any case)"),
    ] = None,
    is_active: Annotated[
        bool | None, Query(description="Only switched on (true) or off (false)")
    ] = None,
    expired: Annotated[
        bool | None, Query(description="Only expired (true) or not expired (false)")
    ] = None,
    search: Annotated[
        str | None, Query(min_length=1, max_length=200, description="Text in the URL or title")
    ] = None,
) -> URLPage:
    """List your short URLs."""
    filters = URLFilters(tag=tag, is_active=is_active, expired=expired, search=search)
    urls, next_cursor = await service.list_page(owner_id, filters, sort, limit, cursor)
    return URLPage(items=[to_url_read(url, settings) for url in urls], next_cursor=next_cursor)


@router.get("/{short_code}", include_in_schema=False)
@router.get(
    "/{short_code}/",
    summary="Get one of your short URLs",
    description="With its owner, tags and click count. Does not count as a visit.",
    responses={**NOT_FOUND, **VALIDATION_ERROR, **INTERNAL_ERROR},
)
async def get_url(
    short_code: ShortCodePath,
    service: URLReadServiceDep,  # read side (replica)
    owner_id: CurrentOwnerId,
    settings: SettingsDep,
) -> URLRead:
    """Get one of your short URLs."""
    url = await service.get(short_code, owner_id)  # someone else's or deleted -> 404
    return to_url_read(url, settings)


@router.put("/{short_code}", include_in_schema=False)
@router.put(
    "/{short_code}/",
    summary="Update one of your short URLs",
    description=(
        "Only the fields you send are changed; `tags` replaces all tags. "
        "The short code and alias cannot change (shared links would break)."
    ),
    responses={**NOT_FOUND, **VALIDATION_ERROR, **INTERNAL_ERROR},
)
async def update_url(
    short_code: ShortCodePath,
    payload: Annotated[URLUpdate, Body(openapi_examples=UPDATE_EXAMPLES)],
    service: URLServiceDep,
    owner_id: CurrentOwnerId,
    settings: SettingsDep,
) -> URLRead:
    """Update one of your short URLs."""
    url = await service.update(short_code, owner_id, payload)
    return to_url_read(url, settings)


@router.delete("/{short_code}", status_code=status.HTTP_204_NO_CONTENT, include_in_schema=False)
@router.delete(
    "/{short_code}/",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete one of your short URLs",
    description=(
        "Soft delete: the link stops working (410) and disappears from your URLs, "
        "but its code is never given to anyone else and its click history is kept."
    ),
    responses={**NOT_FOUND, **VALIDATION_ERROR, **INTERNAL_ERROR},
)
async def delete_url(
    short_code: ShortCodePath, service: URLServiceDep, owner_id: CurrentOwnerId
) -> Response:
    """Delete one of your short URLs."""
    await service.delete(short_code, owner_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
