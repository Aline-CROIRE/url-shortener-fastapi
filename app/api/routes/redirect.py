from fastapi import APIRouter, status
from fastapi.responses import RedirectResponse

from app.api.dependencies import UrlServiceDep
from app.api.responses import NOT_FOUND, STORAGE_UNAVAILABLE, VALIDATION_ERROR

router = APIRouter(tags=["Redirect"])


@router.get(
    "/{short_code}",
    summary="Redirect to the original URL",
    description=(
        "Sends the browser to the original URL with a 302 Found redirect. "
        "Swagger UI follows redirects automatically, so test this in a browser or with curl."
    ),
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    responses={
        status.HTTP_302_FOUND: {"description": "Redirect to the original URL (Location header)"},
        **NOT_FOUND,
        **VALIDATION_ERROR,
        **STORAGE_UNAVAILABLE,
    },
)
async def redirect_to_original(short_code: str, service: UrlServiceDep) -> RedirectResponse:
    record = await service.get_url(short_code)  # raises UrlNotFoundError -> 404
    return RedirectResponse(url=record.original_url, status_code=status.HTTP_302_FOUND)
