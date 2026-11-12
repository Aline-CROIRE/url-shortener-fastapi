from fastapi import APIRouter, Request, status
from fastapi.responses import RedirectResponse

from app.api.dependencies import RedirectServiceDep, UrlServiceDep
from app.api.params import ShortCodePath
from app.api.responses import GONE, INTERNAL_ERROR, NOT_FOUND, STORAGE_UNAVAILABLE, VALIDATION_ERROR
from app.api.visitor import visitor_from_request
from app.core.exceptions import UrlNotFoundError

router = APIRouter(tags=["Redirect"])


@router.get(
    "/{short_code}",
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
        **STORAGE_UNAVAILABLE,
    },
)
async def redirect_to_original(
    short_code: ShortCodePath,
    request: Request,
    service: RedirectServiceDep,
    legacy: UrlServiceDep,
) -> RedirectResponse:
    try:
        target = await service.visit(short_code, visitor_from_request(request))
    except UrlNotFoundError:
        # TODO(F2.3): remove this fallback ("strangler fig"). Links created by the
        # Module 5 POST endpoint still live in memory/Redis until F2.3 moves it to
        # the database. Those visits are not recorded.
        record = await legacy.get_url(short_code)  # raises UrlNotFoundError -> 404
        target = record.original_url
    return RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)
