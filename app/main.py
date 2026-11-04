import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis

from app.api.router import router
from app.api.routes import health, redirect
from app.core.config import Settings, get_settings
from app.storage.memory import InMemoryUrlRepository
from app.storage.redis import RedisUrlRepository

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

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
        description=f"{settings.app_name} API",
        debug=settings.debug,
        lifespan=lifespan,
    )
    # Application-level data: every request can read it via request.app.state
    app.state.settings = settings

    app.include_router(health.router)
    app.include_router(router)
    # Catch-all "/{short_code}" route: must be included LAST so it
    # does not hide /health, /docs, /openapi.json or /api/v1/...
    app.include_router(redirect.router)
    return app


app = create_app()
