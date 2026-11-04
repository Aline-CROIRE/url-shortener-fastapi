from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings
from app.services.url_shortener import UrlShortenerService
from app.storage.base import UrlRepository


def get_settings_from_app(request: Request) -> Settings:
    return request.app.state.settings


def get_repository(request: Request) -> UrlRepository:
    # Created once in create_app and shared by every request
    return request.app.state.repository


def get_url_service(
    repository: Annotated[UrlRepository, Depends(get_repository)],
    settings: Annotated[Settings, Depends(get_settings_from_app)],
) -> UrlShortenerService:
    return UrlShortenerService(repository=repository, base_url=settings.base_url)


# Short alias so endpoints can write: service: UrlServiceDep
UrlServiceDep = Annotated[UrlShortenerService, Depends(get_url_service)]
