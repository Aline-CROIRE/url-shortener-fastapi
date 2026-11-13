"""Health check endpoint for the app, the databases and the cache."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import DbSession, ReadDbSession, RedirectCacheDep, SettingsDep
from app.db.database import ping_database

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Health"])

# True / False / None (not configured) -> what /health shows
STATUS = {True: "ok", False: "down", None: "disabled"}


async def database_is_up(db: DbSession) -> bool:
    """Run the smallest possible query to prove PostgreSQL answers."""
    try:
        await ping_database(db)
    except (SQLAlchemyError, OSError):
        logger.warning("Database health check failed", exc_info=True)
        return False
    return True


async def replica_is_up(db: ReadDbSession, settings: SettingsDep) -> bool | None:
    """Check the read replica; return None when no replica is configured.

    Without a replica, reads use the primary database.
    """
    if not settings.database_replica_url:
        return None
    try:
        await ping_database(db)
    except (SQLAlchemyError, OSError):
        logger.warning("Read replica health check failed", exc_info=True)
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
                    "example": {
                        "status": "healthy",
                        "database": "ok",
                        "replica": "ok",
                        "cache": "ok",
                    }
                }
            },
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Database (or the configured read replica) is down",
            "content": {
                "application/json": {
                    "example": {
                        "status": "unhealthy",
                        "database": "down",
                        "replica": "ok",
                        "cache": "ok",
                    }
                }
            },
        },
    },
)
async def check_health(
    response: Response,
    database_ok: Annotated[bool, Depends(database_is_up)],
    replica_ok: Annotated[bool | None, Depends(replica_is_up)],
    cache: RedirectCacheDep,
) -> dict[str, str]:
    """Return the health status."""
    # The databases decide healthy/unhealthy. A configured replica that is down
    # breaks the read endpoints. The cache does not count: without it the app
    # still works (fail open), just a bit slower.
    healthy = database_ok and replica_ok is not False
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "healthy" if healthy else "unhealthy",
        "database": STATUS[database_ok],
        "replica": STATUS[replica_ok],
        "cache": STATUS[await cache.ping()],
    }
