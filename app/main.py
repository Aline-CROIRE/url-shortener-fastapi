"""Build the FastAPI app: settings, logging, database, cache, middleware and routes."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis
from slowapi.errors import RateLimitExceeded

from app.api.router import router
from app.api.routes import health, metrics, redirect
from app.cache import NullRedirectCache, RedirectCache, RedisRedirectCache
from app.core.config import Settings, get_settings
from app.core.error_handlers import register_exception_handlers
from app.core.logging import setup_logging
from app.core.middleware import (
    BodySizeLimitMiddleware,
    MetricsMiddleware,
    RequestLoggingMiddleware,
    SecurityHeadersMiddleware,
)
from app.core.rate_limit import limiter, rate_limit_exceeded_handler
from app.db.database import create_engine, create_read_engine, create_session_factory
from app.worker.tasks import track_click_task
from app.worker.tracker import CeleryClickTracker

logger = logging.getLogger(__name__)

OPENAPI_TAGS = [
    {"name": "Auth", "description": "Register, log in and refresh JWT tokens."},
    {"name": "URLs", "description": "Create and look up short URLs."},
    {"name": "Analytics", "description": "Click statistics, computed in SQL."},
    {"name": "Redirect", "description": "Open a short link."},
    {"name": "Health", "description": "App, database and cache status."},
]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        settings: Settings to use. When None, they are read from the
            environment and the .env file.
    """
    settings = settings or get_settings()
    setup_logging(settings.debug, settings.log_format)  # once per process (see setup_logging)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Startup: runs once, before the first request.
        # WRITE side: one engine (connection pool) + sessionmaker on the primary
        # Pool size, overflow, timeout: every Gunicorn worker opens its own pool
        pool = (settings.db_pool_size, settings.db_max_overflow, settings.db_pool_timeout)
        engine = create_engine(settings.database_url, settings.db_echo, *pool)
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        # READ side: the replica if DATABASE_REPLICA_URL is set, else the primary's
        # pool; always read-only
        read_engine = create_read_engine(
            engine, settings.database_replica_url, settings.db_echo, *pool
        )
        app.state.read_engine = read_engine
        app.state.read_session_factory = create_session_factory(read_engine)

        # Redirect cache: Redis if REDIS_URL is set, otherwise a cache that does nothing
        redis_client: Redis | None = None
        cache: RedirectCache = NullRedirectCache()
        if settings.redis_url:
            redis_client = Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                # short timeouts: a slow or dead Redis must not slow redirects down
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
            )
            cache = RedisRedirectCache(redis_client, settings.cache_ttl_seconds)
        app.state.redirect_cache = cache
        # Clicks go to the Celery queue; a worker saves them (write-behind)
        app.state.click_tracker = CeleryClickTracker(track_click_task)

        yield  # the app handles requests here

        # Shutdown: runs once, when the server stops
        if redis_client is not None:
            await redis_client.aclose()
        if settings.database_replica_url:
            await read_engine.dispose()  # the replica's own pool
        await engine.dispose()  # close every pooled database connection

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        summary="Turn long URLs into short links.",
        openapi_tags=OPENAPI_TAGS,
        # FastAPI(debug=True) would send stack traces to clients and skip our JSON
        # 500 handler, so it stays off. settings.debug only changes the log level.
        lifespan=lifespan,
    )
    # Application-level data: every request can read it via request.app.state
    app.state.settings = settings

    # Added first = innermost: a 413 for a too-large body is still logged and gets
    # the security headers from the two middlewares around it
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    # Added last = outermost: counts and times every request, 413s and crashes included
    app.add_middleware(MetricsMiddleware)
    register_exception_handlers(app)
    # The login rate limit (slowapi): its decorator finds the limiter on app.state
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(router)
    # Catch-all "/{short_code}" route: must be included LAST so it
    # does not hide /health, /docs, /openapi.json or /api/v1/...
    app.include_router(redirect.router)
    return app


app = create_app()
