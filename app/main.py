import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from app.api.router import router
from app.api.routes import health, redirect
from app.core.config import Settings, get_settings
from app.core.error_handlers import register_exception_handlers
from app.core.logging import setup_logging
from app.core.middleware import RequestLoggingMiddleware
from app.storage.memory import InMemoryUrlRepository
from app.storage.redis import RedisUrlRepository

logger = logging.getLogger(__name__)

API_DESCRIPTION = """
Create short links and redirect visitors to the original URL.

**How to use it**
1. `POST /api/v1/urls` with a long URL to get a short code.
2. Open `/{short_code}` in a browser to be redirected (302).
3. `GET /api/v1/urls/{short_code}` to see the details without redirecting.

**Errors** always use the same JSON shape:
`{"error": {"code": "...", "message": "...", "details": ...}}`

Every response carries an `X-Request-ID` header to match it with the server logs.
"""

OPENAPI_TAGS = [
    {"name": "URLs", "description": "Create short URLs and look up their details (versioned under `/api/v1`)."},
    {"name": "Redirect", "description": "Short links themselves: `/{short_code}` at the root."},
    {"name": "Health", "description": "Liveness and storage check, used by Docker and monitoring."},
]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings.debug)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Startup: runs once, before the first request
        redis_client: Redis | None = None
        if settings.storage_backend == "redis":
            redis_client = Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            app.state.repository = RedisUrlRepository(redis_client)
            if not await app.state.repository.ping():
                logger.warning("Redis is not reachable at %s", settings.redis_url)
        else:
            app.state.repository = InMemoryUrlRepository()
        app.state.redis = redis_client

        yield  # the app handles requests here

        # Shutdown: runs once, when the server stops
        if redis_client is not None:
            await redis_client.aclose()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        summary="A small REST API that turns long URLs into short links.",
        description=API_DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
        # FastAPI(debug=True) would send stack traces to clients and skip our JSON
        # 500 handler, so it stays off. settings.debug only changes the log level.
        lifespan=lifespan,
    )
    # Application-level data: every request can read it via request.app.state
    app.state.settings = settings

    app.add_middleware(RequestLoggingMiddleware)
    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(router)
    # Catch-all "/{short_code}" route: must be included LAST so it
    # does not hide /health, /docs, /openapi.json or /api/v1/...
    app.include_router(redirect.router)
    return app


app = create_app()
