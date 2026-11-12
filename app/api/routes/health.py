import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import DbSession
from app.db.database import ping_database

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
            "content": {"application/json": {"example": {"status": "healthy", "database": "ok"}}}
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Database is down",
            "content": {
                "application/json": {"example": {"status": "unhealthy", "database": "down"}}
            },
        },
    },
)
async def check_health(
    response: Response,
    database_ok: Annotated[bool, Depends(database_is_up)],
) -> dict[str, str]:
    if not database_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "healthy" if database_ok else "unhealthy",
        "database": "ok" if database_ok else "down",
    }
