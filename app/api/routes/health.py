from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from app.api.dependencies import get_repository, get_settings_from_app
from app.core.config import Settings
from app.storage.base import UrlRepository

router = APIRouter(tags=["Health"])


@router.get(
    "/health",
    summary="Health check",
    responses={
        status.HTTP_200_OK: {
            "content": {"application/json": {"example": {"status": "healthy", "storage": "redis"}}}
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Redis is down",
            "content": {"application/json": {"example": {"status": "unhealthy", "storage": "redis"}}},
        },
    },
)
async def check_health(
    response: Response,
    repository: Annotated[UrlRepository, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> dict[str, str]:
    if not await repository.ping():
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unhealthy", "storage": settings.storage_backend}
    return {"status": "healthy", "storage": settings.storage_backend}
