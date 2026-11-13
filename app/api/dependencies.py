"""FastAPI dependencies: settings, database sessions, cache, repositories and services."""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import RedirectCache
from app.core.config import Settings
from app.db.database import get_db, get_read_db
from app.repositories import ClickRepository, TagRepository, URLRepository, UserRepository
from app.services.analytics import AnalyticsService
from app.services.redirect import RedirectService
from app.services.urls import URLService


def get_settings_from_app(request: Request) -> Settings:
    """Return the settings stored on the app at startup."""
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(get_settings_from_app)]

# Two kinds of session (see app/db/database.py):
#   DbSession     = WRITE: the primary database (also for reads that must see fresh writes)
#   ReadDbSession = READ ONLY: the replica if configured, maybe a moment behind
DbSession = Annotated[AsyncSession, Depends(get_db)]
ReadDbSession = Annotated[AsyncSession, Depends(get_read_db)]


def get_redirect_cache(request: Request) -> RedirectCache:
    """Return the redirect cache created at startup."""
    # Created once in the app lifespan (Redis, or a no-op cache without REDIS_URL)
    return request.app.state.redirect_cache


RedirectCacheDep = Annotated[RedirectCache, Depends(get_redirect_cache)]


# ---------- WRITE side: repositories on the primary (one per request, same session) ----------


def get_user_repository(db: DbSession) -> UserRepository:
    """Return a user repository on the write session."""
    return UserRepository(db)


def get_url_repository(db: DbSession) -> URLRepository:
    """Return a URL repository on the write session."""
    return URLRepository(db)


def get_click_repository(db: DbSession) -> ClickRepository:
    """Return a click repository on the write session."""
    return ClickRepository(db)


def get_tag_repository(db: DbSession) -> TagRepository:
    """Return a tag repository on the write session."""
    return TagRepository(db)


UserRepoDep = Annotated[UserRepository, Depends(get_user_repository)]
URLRepoDep = Annotated[URLRepository, Depends(get_url_repository)]
ClickRepoDep = Annotated[ClickRepository, Depends(get_click_repository)]
TagRepoDep = Annotated[TagRepository, Depends(get_tag_repository)]


# ---------- READ side: the same repositories, on the read-only session ----------


def get_read_url_repository(db: ReadDbSession) -> URLRepository:
    """Return a URL repository on the read-only session."""
    return URLRepository(db)


def get_read_click_repository(db: ReadDbSession) -> ClickRepository:
    """Return a click repository on the read-only session."""
    return ClickRepository(db)


ReadURLRepoDep = Annotated[URLRepository, Depends(get_read_url_repository)]
ReadClickRepoDep = Annotated[ClickRepository, Depends(get_read_click_repository)]


# ---------- services (they own the transactions) ----------


def get_redirect_service(
    db: DbSession, urls: URLRepoDep, clicks: ClickRepoDep, cache: RedirectCacheDep
) -> RedirectService:
    """Build the redirect service (write side)."""
    # WRITE side: a redirect records a click, and must see a link created a moment ago
    return RedirectService(session=db, urls=urls, clicks=clicks, cache=cache)


RedirectServiceDep = Annotated[RedirectService, Depends(get_redirect_service)]


def get_url_service(
    db: DbSession,
    urls: URLRepoDep,
    users: UserRepoDep,
    tags: TagRepoDep,
    cache: RedirectCacheDep,
) -> URLService:
    """Build the URL service for create, update and delete (write side)."""
    # WRITE side: create / update / delete
    return URLService(session=db, urls=urls, users=users, tags=tags, cache=cache)


URLServiceDep = Annotated[URLService, Depends(get_url_service)]


def get_url_read_service(db: ReadDbSession, urls: ReadURLRepoDep) -> URLService:
    """Build the URL service for list and get (read-only side)."""
    # READ side: list and get only. Its users/tags repositories are never used by
    # those methods; any write attempted here would be refused (read-only session).
    return URLService(session=db, urls=urls, users=UserRepository(db), tags=TagRepository(db))


URLReadServiceDep = Annotated[URLService, Depends(get_url_read_service)]


def get_analytics_service(urls: ReadURLRepoDep, clicks: ReadClickRepoDep) -> AnalyticsService:
    """Build the analytics service (read-only side)."""
    # READ side: analytics are only counting; a fraction of a second behind is fine
    return AnalyticsService(urls=urls, clicks=clicks)


AnalyticsServiceDep = Annotated[AnalyticsService, Depends(get_analytics_service)]
