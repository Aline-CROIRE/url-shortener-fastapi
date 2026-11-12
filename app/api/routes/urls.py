from typing import Annotated

from fastapi import APIRouter, Body, Response, status

from app.api.dependencies import SettingsDep, URLServiceDep
from app.api.owner import CurrentOwnerId
from app.api.params import ShortCodePath
from app.api.responses import CONFLICT, INTERNAL_ERROR, NOT_FOUND, VALIDATION_ERROR
from app.core.config import Settings
from app.models import URL
from app.schemas.url import URLCreate, URLRead, URLUpdate

router = APIRouter(prefix="/urls", tags=["URLs"])

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
    # short_url is built from base_url (a setting), passed as validation context
    return URLRead.model_validate(url, context={"base_url": settings.base_url})


@router.post(
    "",
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
    url = await service.create(payload, owner_id)
    return to_url_read(url, settings)


@router.get(
    "/{short_code}",
    summary="Get one of your short URLs",
    description="With its owner, tags and click count. Does not count as a visit.",
    responses={**NOT_FOUND, **VALIDATION_ERROR, **INTERNAL_ERROR},
)
async def get_url(
    short_code: ShortCodePath,
    service: URLServiceDep,
    owner_id: CurrentOwnerId,
    settings: SettingsDep,
) -> URLRead:
    url = await service.get(short_code, owner_id)  # someone else's or deleted -> 404
    return to_url_read(url, settings)


@router.put(
    "/{short_code}",
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
    url = await service.update(short_code, owner_id, payload)
    return to_url_read(url, settings)


@router.delete(
    "/{short_code}",
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
    await service.delete(short_code, owner_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
