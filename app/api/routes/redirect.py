"""Endpoint that sends a visitor from a short code to the original URL."""

from fastapi import APIRouter, Request, status
from fastapi.responses import RedirectResponse

from app.api.dependencies import RedirectServiceDep
from app.api.params import ShortCodePath
from app.api.responses import GONE, INTERNAL_ERROR, NOT_FOUND, VALIDATION_ERROR
from app.api.visitor import visitor_from_request

router = APIRouter(tags=["Redirect"])


# "/{short_code}/" (specification, documented) and "/{short_code}" (the shortest link)
@router.get(
    "/{short_code}",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    include_in_schema=False,
)
@router.get(
    "/{short_code}/",
    summary="Redirect to the original URL",
    description=(
        "Only active, non-expired links redirect; each visit is recorded. "
        "Swagger shows *Failed to fetch* here because the browser follows the redirect. "
        "Open the short link in a browser tab instead."
    ),
    # 302 (not 301): browsers cache a 301 and skip our server, so clicks would be lost
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    responses={
        status.HTTP_302_FOUND: {
            "description": "Redirect",
            "headers": {
                "Location": {
                    "description": "The original URL",
                    "schema": {"type": "string", "example": "https://www.python.org/about/"},
                }
            },
        },
        **NOT_FOUND,
        **GONE,
        **VALIDATION_ERROR,
        **INTERNAL_ERROR,
    },
)
async def redirect_to_original(
    short_code: ShortCodePath,
    request: Request,
    service: RedirectServiceDep,
) -> RedirectResponse:
    """Redirect to the original URL."""
    # 404 unknown, 410 deleted/switched off/expired; otherwise the visit is recorded
    target = await service.visit(short_code, visitor_from_request(request))
    return RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)
