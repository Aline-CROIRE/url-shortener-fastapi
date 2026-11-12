from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import RedirectCache
from app.core.config import Settings
from app.db.database import get_db
from app.repositories import ClickRepository, TagRepository, URLRepository, UserRepository
from app.services.analytics import AnalyticsService
from app.services.redirect import RedirectService
from app.services.urls import URLService


def get_settings_from_app(request: Request) -> Settings:
    return request.app.state.settings


# Short alias so endpoints can write: db: DbSession
DbSession = Annotated[AsyncSession, Depends(get_db)]


def get_redirect_cache(request: Request) -> RedirectCache:
    # Created once in the app lifespan (Redis, or a no-op cache without REDIS_URL)
    return request.app.state.redirect_cache


RedirectCacheDep = Annotated[RedirectCache, Depends(get_redirect_cache)]


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


# ---------- services (they own the transactions) ----------


def get_redirect_service(
    db: DbSession, urls: URLRepoDep, clicks: ClickRepoDep, cache: RedirectCacheDep
) -> RedirectService:
    return RedirectService(session=db, urls=urls, clicks=clicks, cache=cache)


RedirectServiceDep = Annotated[RedirectService, Depends(get_redirect_service)]


def get_url_service(
    db: DbSession,
    urls: URLRepoDep,
    users: UserRepoDep,
    tags: TagRepoDep,
    cache: RedirectCacheDep,
) -> URLService:
    return URLService(session=db, urls=urls, users=users, tags=tags, cache=cache)


URLServiceDep = Annotated[URLService, Depends(get_url_service)]
SettingsDep = Annotated[Settings, Depends(get_settings_from_app)]


def get_analytics_service(urls: URLRepoDep, clicks: ClickRepoDep) -> AnalyticsService:
    return AnalyticsService(urls=urls, clicks=clicks)


AnalyticsServiceDep = Annotated[AnalyticsService, Depends(get_analytics_service)]
