from fastapi import FastAPI

from app.api.router import router
from app.api.routes import health, redirect
from app.core.config import Settings, get_settings
from app.storage.memory import InMemoryUrlRepository


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=f"{settings.app_name} API",
        debug=settings.debug,
    )
    # Application-level data: every request can read it via request.app.state
    app.state.settings = settings
    app.state.repository = InMemoryUrlRepository()

    app.include_router(health.router)
    app.include_router(router)
    # Catch-all "/{short_code}" route: must be included LAST so it
    # does not hide /health, /docs, /openapi.json or /api/v1/...
    app.include_router(redirect.router)
    return app


app = create_app()
