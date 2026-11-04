from fastapi import APIRouter, HTTPException, status
from fastapi.responses import RedirectResponse

from app.storage.memory import url_store

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
        status.HTTP_404_NOT_FOUND: {"description": "Short code does not exist"},
    },
)
async def redirect_to_original(short_code: str) -> RedirectResponse:
    saved_url = url_store.get(short_code)

    if saved_url is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Short URL not found",
        )

    return RedirectResponse(
        url=saved_url["original_url"],
        status_code=status.HTTP_302_FOUND,
    )
