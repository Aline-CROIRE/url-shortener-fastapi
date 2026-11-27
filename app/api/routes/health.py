"""Health probes: is the process alive (liveness), and can it serve requests (readiness)?

LIVENESS  /health/live   "Is the process still answering?" Checks nothing else.
                         If it fails, the orchestrator (Docker, Kubernetes) RESTARTS
                         the container. It must not check the database: restarting
                         the app never fixes a database outage, it only adds a
                         restart storm on top.
READINESS /health/ready  "Can it serve real requests right now?" Checks PostgreSQL
                         (SELECT 1) and Redis (PING). If it fails, the load balancer
                         sends no traffic here until it passes again; nothing restarts.
/health                  The spec's endpoint: the same check as /health/ready.
"""

import asyncio
import logging
from collections.abc import Awaitable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.exc import SQLAlchemyError

from app.api.dependencies import DbSession, ReadDbSession, RedirectCacheDep, SettingsDep
from app.db.database import ping_database

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Health"])

# True / False / None (not configured) -> what the probe shows
STATUS = {True: "ok", False: "down", None: "disabled"}
# A probe must answer before the orchestrator's own timeout (Docker: 5 s here).
# A database that needs longer than this for SELECT 1 counts as down.
CHECK_TIMEOUT_SECONDS = 2.0
# A proxy that cached "healthy" would hide an outage
NO_STORE = {"Cache-Control": "no-store"}
CHECK_ERRORS = (SQLAlchemyError, OSError, TimeoutError)


async def within_timeout(check: Awaitable[Any]) -> Any:
    """Run one check, giving up after CHECK_TIMEOUT_SECONDS (raises TimeoutError)."""
    async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
        return await check


async def database_is_up(db: DbSession) -> bool:
    """Run the smallest possible query to prove PostgreSQL answers."""
    try:
        await within_timeout(ping_database(db))
    except CHECK_ERRORS:
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
        await within_timeout(ping_database(db))
    except CHECK_ERRORS:
        logger.warning("Read replica health check failed", exc_info=True)
        return False
    return True


async def cache_is_up(cache: RedirectCacheDep) -> bool | None:
    """PING Redis; None when no REDIS_URL is configured."""
    try:
        result: bool | None = await within_timeout(cache.ping())
        return result
    except TimeoutError:
        logger.warning("Redis health check timed out")
        return False


READY_EXAMPLE = {"status": "healthy", "database": "ok", "replica": "ok", "cache": "ok"}
NOT_READY_EXAMPLE = {"status": "unhealthy", "database": "ok", "replica": "ok", "cache": "down"}


@router.get(
    "/health/live",
    summary="Liveness probe",
    description="200 while the process answers. Checks no database or cache on purpose.",
)
async def check_live(response: Response) -> dict[str, str]:
    """Return 200: the process is alive."""
    response.headers.update(NO_STORE)
    return {"status": "alive"}


@router.get(
    "/health",
    summary="Health check (readiness)",
    responses={
        status.HTTP_200_OK: {
            "description": "PostgreSQL and Redis answer",
            "content": {"application/json": {"example": READY_EXAMPLE}},
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "PostgreSQL, the configured replica, or Redis is down",
            "content": {"application/json": {"example": NOT_READY_EXAMPLE}},
        },
    },
)
@router.get("/health/ready", summary="Readiness probe (same check as /health)")
async def check_ready(
    response: Response,
    database_ok: Annotated[bool, Depends(database_is_up)],
    replica_ok: Annotated[bool | None, Depends(replica_is_up)],
    cache_ok: Annotated[bool | None, Depends(cache_is_up)],
) -> dict[str, str]:
    """Return 200 when every configured component answers, else 503."""
    response.headers.update(NO_STORE)
    # Module 8: Redis counts too. It is the cache AND Celery's job queue; without
    # it every click is lost. "disabled" (no REDIS_URL) or no replica is fine.
    healthy = database_ok and replica_ok is not False and cache_ok is not False
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "healthy" if healthy else "unhealthy",
        "database": STATUS[database_ok],
        "replica": STATUS[replica_ok],
        "cache": STATUS[cache_ok],
    }
