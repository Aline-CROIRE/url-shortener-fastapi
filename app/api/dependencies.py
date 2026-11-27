"""FastAPI dependencies: settings, sessions, cache, click tracker, repositories and services."""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import RedirectCache
from app.core.config import Settings
from app.db.database import get_db, get_read_db
from app.repositories import (
    ClickRepository,
    RefreshTokenRepository,
    TagRepository,
    URLRepository,
    UserRepository,
)
from app.services.analytics import AnalyticsService
from app.services.auth import AuthService
from app.services.clicks import ClickTracker
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


def get_click_tracker(request: Request) -> ClickTracker:
    """Return the click tracker created at startup (sends clicks to Celery)."""
    return request.app.state.click_tracker


ClickTrackerDep = Annotated[ClickTracker, Depends(get_click_tracker)]


# ---------- WRITE side: repositories on the primary (one per request, same session) ----------


def get_user_repository(db: DbSession) -> UserRepository:
    """Return a user repository on the write session."""
    return UserRepository(db)


def get_url_repository(db: DbSession) -> URLRepository:
    """Return a URL repository on the write session."""
    return URLRepository(db)


def get_tag_repository(db: DbSession) -> TagRepository:
    """Return a tag repository on the write session."""
    return TagRepository(db)


UserRepoDep = Annotated[UserRepository, Depends(get_user_repository)]
URLRepoDep = Annotated[URLRepository, Depends(get_url_repository)]
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
    db: DbSession, urls: URLRepoDep, cache: RedirectCacheDep
) -> RedirectService:
    """Build the redirect service (it only reads)."""
    # The PRIMARY, not the replica: a link created a moment ago must redirect at once
    return RedirectService(session=db, urls=urls, cache=cache)


RedirectServiceDep = Annotated[RedirectService, Depends(get_redirect_service)]


def get_url_service(
    db: DbSession,
    urls: URLRepoDep,
    users: UserRepoDep,
    tags: TagRepoDep,
    cache: RedirectCacheDep,
    settings: SettingsDep,
) -> URLService:
    """Build the URL service for create, update and delete (write side)."""
    # WRITE side: create / update / delete. own_hosts lets it refuse links to itself.
    return URLService(
        session=db,
        urls=urls,
        users=users,
        tags=tags,
        cache=cache,
        own_hosts=settings.own_host_names,
    )


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


def get_auth_service(db: DbSession, users: UserRepoDep, settings: SettingsDep) -> AuthService:
    """Build the auth service (write side: register, login and refresh write to the database)."""
    return AuthService(
        session=db, users=users, settings=settings, refresh_tokens=RefreshTokenRepository(db)
    )


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
