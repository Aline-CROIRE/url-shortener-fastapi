from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request, status

from app.schemas.url import URLCreate, URLRead
from app.storage.memory import url_store
from app.utils.codes import generate_code

router = APIRouter(prefix="/urls", tags=["URLs"])


def build_short_url(request: Request, short_code: str) -> str:
    base_url = request.app.state.settings.base_url.rstrip("/")
    return f"{base_url}/{short_code}"


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Create a short URL",
    description="Takes a long URL and returns a new 7-character short code and short link.",
)
async def create_short_url(payload: URLCreate, request: Request) -> URLRead:
    short_code = generate_code()
    while short_code in url_store:
        short_code = generate_code()

    saved_url = {
        "short_code": short_code,
        "original_url": str(payload.original_url),
        "created_at": datetime.now(timezone.utc),
    }
    url_store[short_code] = saved_url

    return URLRead(**saved_url, short_url=build_short_url(request, short_code))


@router.get(
    "/{short_code}",
    summary="Get short URL info",
    description="Returns the details of a short code without redirecting.",
    responses={status.HTTP_404_NOT_FOUND: {"description": "Short code does not exist"}},
)
async def get_short_url(short_code: str, request: Request) -> URLRead:
    saved_url = url_store.get(short_code)

    if saved_url is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Short URL not found",
        )

    return URLRead(**saved_url, short_url=build_short_url(request, short_code))
