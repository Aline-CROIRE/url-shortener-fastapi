from fastapi import APIRouter, status
from fastapi.responses import RedirectResponse

from app.api.dependencies import UrlServiceDep
from app.api.params import ShortCodePath
from app.api.responses import INTERNAL_ERROR, NOT_FOUND, STORAGE_UNAVAILABLE, VALIDATION_ERROR

router = APIRouter(tags=["Redirect"])


@router.get(
    "/{short_code}",
    summary="Redirect to the original URL",
    description=(
        "Swagger shows *Failed to fetch* here because the browser follows the redirect. "
        "Open the short link in a browser tab instead."
    ),
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
        **VALIDATION_ERROR,
        **INTERNAL_ERROR,
        **STORAGE_UNAVAILABLE,
    },
)
async def redirect_to_original(short_code: ShortCodePath, service: UrlServiceDep) -> RedirectResponse:
    record = await service.get_url(short_code)  # raises UrlNotFoundError -> 404
    return RedirectResponse(url=record.original_url, status_code=status.HTTP_302_FOUND)
