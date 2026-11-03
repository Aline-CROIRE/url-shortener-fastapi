from fastapi import FastAPI
from app.core.config import get_settings
from app.api.routes import health
from app.api.router import router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=f"{settings.app_name} API",
        debug=settings.debug,
    )
    app.include_router(health.router)
    app.include_router(router)
    return app

app = create_app()
