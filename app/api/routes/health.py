"""Health probes (Kubernetes' three): has it started, is it alive, can it serve requests?

STARTUP   /health/startup "Has this instance finished starting?" PostgreSQL answers and
                         its schema is at the newest migration. Until it passes, the
                         orchestrator neither checks liveness nor sends traffic, so a slow
                         start is not mistaken for a hang.
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
from typing import Any

from fastapi import APIRouter, Response, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import DbSession, ReadDbSession, RedirectCacheDep, SettingsDep
from app.cache import RedirectCache
from app.db.database import ping_database
from app.db.migrations import current_schema_revision, expected_schema_head

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


async def database_is_up(db: AsyncSession) -> bool:
    """Run the smallest possible query to prove PostgreSQL answers."""
    try:
        await within_timeout(ping_database(db))
    except CHECK_ERRORS:
        logger.warning("Database health check failed", exc_info=True)
        return False
    return True


async def replica_is_up(db: AsyncSession, replica_url: str) -> bool | None:
    """Check the read replica; return None when no replica is configured.

    Without a replica, reads use the primary database.
    """
    if not replica_url:
        return None
    try:
        await within_timeout(ping_database(db))
    except CHECK_ERRORS:
        logger.warning("Read replica health check failed", exc_info=True)
        return False
    return True


async def cache_is_up(cache: RedirectCache) -> bool | None:
    """PING Redis; None when no REDIS_URL is configured."""
    try:
        result: bool | None = await within_timeout(cache.ping())
        return result
    except TimeoutError:
        logger.warning("Redis health check timed out")
        return False


READY_EXAMPLE = {"status": "healthy", "database": "ok", "replica": "ok", "cache": "ok"}
NOT_READY_EXAMPLE = {"status": "unhealthy", "database": "ok", "replica": "ok", "cache": "down"}


SCHEMA_STATUS_DOWN = "down"


async def schema_status(db: AsyncSession) -> str:
    """Return "current", "outdated" (migrations not applied yet) or "down"."""
    try:
        revision = await within_timeout(current_schema_revision(db))
    except CHECK_ERRORS:
        logger.warning("Startup check: database not reachable", exc_info=True)
        return SCHEMA_STATUS_DOWN
    return "current" if revision == expected_schema_head() else "outdated"


@router.get(
    "/health/startup",
    summary="Startup probe",
    description=(
        "200 once this instance can start serving: PostgreSQL answers and its schema is "
        "at the newest migration. Until then Kubernetes neither checks liveness nor "
        "sends traffic."
    ),
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "Still starting: database down, or migrations not applied yet",
            "content": {
                "application/json": {
                    "example": {"status": "starting", "database_schema": "outdated"}
                }
            },
        }
    },
)
async def check_startup(response: Response, db: DbSession) -> dict[str, str]:
    """Return 200 when the database schema is current, else 503."""
    response.headers.update(NO_STORE)
    schema = await schema_status(db)
    if schema != "current":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "starting", "database_schema": schema}
    return {"status": "started", "database_schema": schema}


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
    db: DbSession,
    read_db: ReadDbSession,
    cache: RedirectCacheDep,
    settings: SettingsDep,
) -> dict[str, str]:
    """Return 200 when every configured component answers, else 503."""
    response.headers.update(NO_STORE)
    # asyncio.gather: start all three checks at once and wait for all of them.
    # Each one mostly WAITS (network); while one waits, the others run. Worst
    # case = the slowest check (2 s), not the sum of all three (6 s).
    # (db and read_db are separate sessions: one connection each, never shared.)
    database_ok, replica_ok, cache_ok = await asyncio.gather(
        database_is_up(db),
        replica_is_up(read_db, settings.database_replica_url),
        cache_is_up(cache),
    )
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
