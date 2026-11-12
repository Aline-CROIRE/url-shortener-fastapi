import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import DbSession, get_repository, get_settings_from_app
from app.core.config import Settings
from app.db.database import ping_database
from app.storage.base import UrlRepository

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Health"])


async def database_is_up(db: DbSession) -> bool:
    """Run the smallest possible query to prove PostgreSQL answers."""
    try:
        await ping_database(db)
    except (SQLAlchemyError, OSError):
        logger.warning("Database health check failed", exc_info=True)
        return False
    return True


@router.get(
    "/health",
    summary="Health check",
    responses={
        status.HTTP_200_OK: {
            "content": {
                "application/json": {
                    "example": {"status": "healthy", "storage": "redis", "database": "ok"}
                }
            }
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Storage or database is down",
            "content": {
                "application/json": {
                    "example": {"status": "unhealthy", "storage": "redis", "database": "down"}
                }
            },
        },
    },
)
async def check_health(
    response: Response,
    repository: Annotated[UrlRepository, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
    database_ok: Annotated[bool, Depends(database_is_up)],
) -> dict[str, str]:
    storage_ok = await repository.ping()
    healthy = storage_ok and database_ok
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "healthy" if healthy else "unhealthy",
        "storage": settings.storage_backend,
        "database": "ok" if database_ok else "down",
    }
