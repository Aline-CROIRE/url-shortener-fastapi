import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import DbSession, RedirectCacheDep
from app.db.database import ping_database

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Health"])

# cache.ping() -> True / False / None (no cache configured)
CACHE_STATUS = {True: "ok", False: "down", None: "disabled"}


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
            "description": "Healthy (a cache that is down only makes redirects slower)",
            "content": {
                "application/json": {
                    "example": {"status": "healthy", "database": "ok", "cache": "ok"}
                }
            },
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Database is down",
            "content": {
                "application/json": {
                    "example": {"status": "unhealthy", "database": "down", "cache": "ok"}
                }
            },
        },
    },
)
async def check_health(
    response: Response,
    database_ok: Annotated[bool, Depends(database_is_up)],
    cache: RedirectCacheDep,
) -> dict[str, str]:
    # Only the database decides healthy/unhealthy: without the cache the app still
    # works (fail open), it is just a bit slower
    if not database_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "healthy" if database_ok else "unhealthy",
        "database": "ok" if database_ok else "down",
        "cache": CACHE_STATUS[await cache.ping()],
    }
