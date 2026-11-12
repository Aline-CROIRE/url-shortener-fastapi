from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.database import get_db
from app.repositories import ClickRepository, TagRepository, URLRepository, UserRepository
from app.services.url_shortener import UrlShortenerService
from app.storage.base import UrlRepository


def get_settings_from_app(request: Request) -> Settings:
    return request.app.state.settings


# Short alias so endpoints can write: db: DbSession
DbSession = Annotated[AsyncSession, Depends(get_db)]


# ---------- database repositories (one per request, sharing its session) ----------


def get_user_repository(db: DbSession) -> UserRepository:
    return UserRepository(db)


def get_url_repository(db: DbSession) -> URLRepository:
    return URLRepository(db)


def get_click_repository(db: DbSession) -> ClickRepository:
    return ClickRepository(db)


def get_tag_repository(db: DbSession) -> TagRepository:
    return TagRepository(db)


UserRepoDep = Annotated[UserRepository, Depends(get_user_repository)]
URLRepoDep = Annotated[URLRepository, Depends(get_url_repository)]
ClickRepoDep = Annotated[ClickRepository, Depends(get_click_repository)]
TagRepoDep = Annotated[TagRepository, Depends(get_tag_repository)]


# ---------- Module 5 storage (memory/Redis), still used by the URL routes ----------
# TODO(F2.3): remove when the URL routes switch to the database repositories.


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
