"""Endpoints to create, list, get, update and delete short URLs."""

from typing import Annotated

from fastapi import APIRouter, Body, Query, Request, Response, status
from pydantic import AfterValidator

from app.api.auth import CurrentUser
from app.api.dependencies import SettingsDep, URLReadServiceDep, URLServiceDep
from app.api.params import ShortCodePath
from app.api.permissions import is_admin
from app.api.responses import (
    CONFLICT,
    FORBIDDEN,
    INTERNAL_ERROR,
    INVALID_CURSOR,
    NOT_FOUND,
    TOO_MANY_REQUESTS,
    UNAUTHORIZED,
    VALIDATION_ERROR,
)
from app.core.config import Settings
from app.core.rate_limit import create_url_rate_limit, limiter, user_rate_limit_key
from app.models import URL
from app.models.tag import TAG_NAME_MAX_LENGTH
from app.repositories.url import MAX_LIMIT, URLFilters, URLSort
from app.schemas.base import refuse_nul
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
    description=(
        "URL and tags are saved in one transaction. A custom alias becomes the short code. "
        "At most 30 new URLs per minute per user (429 after that)."
    ),
    responses={
        **UNAUTHORIZED,
        **FORBIDDEN,
        **CONFLICT,
        **TOO_MANY_REQUESTS,
        **VALIDATION_ERROR,
        **INTERNAL_ERROR,
    },
)
# Per USER (the token's), not per IP: stops mass creation of spam or phishing links,
# also by a create/delete loop. Runs after get_current_user, so anonymous calls get
# 401 and are never counted.
@limiter.shared_limit(create_url_rate_limit, scope="create_url", key_func=user_rate_limit_key)
async def create_url(
    request: Request,  # slowapi reads the user id stored on it
    payload: Annotated[URLCreate, Body(openapi_examples=CREATE_EXAMPLES)],
    service: URLServiceDep,
    user: CurrentUser,
    settings: SettingsDep,
) -> URLRead:
    """Create a short URL."""
    url = await service.create(payload, user.id)  # the owner always comes from the token
    return to_url_read(url, settings)


@router.get("", include_in_schema=False)
@router.get(
    "/",
    summary="List your short URLs",
    description=(
        "Keyset (cursor) pagination: pass `next_cursor` from one page as `cursor` to get "
        "the next. `most_clicked` is one top list (up to `limit` URLs, no next page), "
        "because click counts change while you page. Filters are combined with AND and "
        "only applied when given."
    ),
    responses={**UNAUTHORIZED, **INVALID_CURSOR, **VALIDATION_ERROR, **INTERNAL_ERROR},
)
async def list_urls(
    service: URLReadServiceDep,  # read side (replica)
    user: CurrentUser,
    settings: SettingsDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT, description="Page size")] = 20,
    cursor: Annotated[str | None, Query(description="next_cursor from the previous page")] = None,
    sort: Annotated[
        URLSort, Query(description="newest (has pages) or most_clicked (one top list)")
    ] = URLSort.NEWEST,
    tag: Annotated[
        str | None,
        Query(min_length=1, max_length=TAG_NAME_MAX_LENGTH, description="Tag name (any case)"),
        AfterValidator(refuse_nul),
    ] = None,
    is_active: Annotated[
        bool | None, Query(description="Only switched on (true) or off (false)")
    ] = None,
    expired: Annotated[
        bool | None, Query(description="Only expired (true) or not expired (false)")
    ] = None,
    search: Annotated[
        str | None,
        Query(min_length=1, max_length=200, description="Text in the URL or title"),
        AfterValidator(refuse_nul),
    ] = None,
) -> URLPage:
    """List your short URLs."""
    filters = URLFilters(tag=tag, is_active=is_active, expired=expired, search=search)
    # Only your own URLs, also for an Admin: a list is "mine", not "everyone's"
    urls, next_cursor = await service.list_page(user.id, filters, sort, limit, cursor)
    return URLPage(items=[to_url_read(url, settings) for url in urls], next_cursor=next_cursor)


@router.get("/{short_code}", include_in_schema=False)
@router.get(
    "/{short_code}/",
    summary="Get one of your short URLs (Admin: any)",
    description="With its owner, tags and click count. Does not count as a visit.",
    responses={**UNAUTHORIZED, **FORBIDDEN, **NOT_FOUND, **VALIDATION_ERROR, **INTERNAL_ERROR},
)
async def get_url(
    short_code: ShortCodePath,
    service: URLReadServiceDep,  # read side (replica)
    user: CurrentUser,
    settings: SettingsDep,
) -> URLRead:
    """Get one of your short URLs."""
    url = await service.get(short_code, user.id, is_admin=is_admin(user))
    return to_url_read(url, settings)


@router.put("/{short_code}", include_in_schema=False)
@router.put(
    "/{short_code}/",
    summary="Update one of your short URLs (Admin: any)",
    description=(
        "Only the fields you send are changed; `tags` replaces all tags. "
        "The short code and alias cannot change (shared links would break)."
    ),
    responses={**UNAUTHORIZED, **FORBIDDEN, **NOT_FOUND, **VALIDATION_ERROR, **INTERNAL_ERROR},
)
async def update_url(
    short_code: ShortCodePath,
    payload: Annotated[URLUpdate, Body(openapi_examples=UPDATE_EXAMPLES)],
    service: URLServiceDep,
    user: CurrentUser,
    settings: SettingsDep,
) -> URLRead:
    """Update one of your short URLs."""
    url = await service.update(short_code, user.id, payload, is_admin=is_admin(user))
    return to_url_read(url, settings)


@router.delete("/{short_code}", status_code=status.HTTP_204_NO_CONTENT, include_in_schema=False)
@router.delete(
    "/{short_code}/",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete one of your short URLs (Admin: any)",
    description=(
        "Soft delete: the link stops working (410) and disappears from your URLs, "
        "but its code is never given to anyone else and its click history is kept."
    ),
    responses={**UNAUTHORIZED, **FORBIDDEN, **NOT_FOUND, **VALIDATION_ERROR, **INTERNAL_ERROR},
)
async def delete_url(
    short_code: ShortCodePath, service: URLServiceDep, user: CurrentUser
) -> Response:
    """Delete one of your short URLs."""
    await service.delete(short_code, user.id, is_admin=is_admin(user))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
