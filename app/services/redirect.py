"""The redirect service: turn a short code into its original URL and the click to record."""

from dataclasses import dataclass
from datetime import datetime
from ipaddress import IPv4Address, IPv6Address

from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import CachedRedirect, NullRedirectCache, RedirectCache
from app.core.exceptions import UrlGoneError, UrlNotFoundError
from app.repositories import URLRepository
from app.repositories.url import utc_now
from app.services.clicks import ClickEvent


@dataclass(frozen=True)
class VisitorInfo:
    """What we record about one visit (no HTTP objects: the service stays testable)."""

    ip_address: IPv4Address | IPv6Address | None = None
    user_agent: str | None = None
    referrer: str | None = None


@dataclass(frozen=True)
class Visit:
    """The result of a visit: where to send the visitor, and the click to record."""

    original_url: str
    click: ClickEvent


class RedirectService:
    """Handles one visit of a short link. It only READS; it never writes.

    CACHE-ASIDE: look in the cache first; on a hit no database is needed at all;
    on a miss read the database and fill the cache.

    WRITE-BEHIND: the click is not saved here. visit() returns it, the route hands
    it to Celery after the response, and a worker saves it (app/worker/tasks.py).
    """

    def __init__(
        self,
        session: AsyncSession,
        urls: URLRepository,
        cache: RedirectCache | None = None,
    ) -> None:
        """Keep the session, repository and cache (no cache -> a cache that stores nothing)."""
        self._session = session
        self._urls = urls
        self._cache = cache or NullRedirectCache()

    async def visit(
        self, short_code: str, visitor: VisitorInfo, now: datetime | None = None
    ) -> Visit:
        """Return where to redirect and the click to record.

        Raises:
            UrlNotFoundError: If no URL has this short code (404).
            UrlGoneError: If the URL is deleted, switched off or expired (410).
        """
        now = now or utc_now()
        entry = await self._live_entry(short_code, now)
        click = ClickEvent(
            url_id=entry.url_id,
            ip_address=str(visitor.ip_address) if visitor.ip_address else None,
            user_agent=visitor.user_agent,
            referrer=visitor.referrer,
            clicked_at=now,
        )
        return Visit(entry.original_url, click)

    async def _live_entry(self, short_code: str, now: datetime) -> CachedRedirect:
        """Find the link: from the cache if possible, else from the database."""
        cached = await self._cache.get(short_code)
        if cached is not None and cached.is_expired(now):
            await self._cache.invalidate(short_code)  # expired while cached: ask the DB
            cached = None
        if cached is not None:
            return cached  # cache HIT: no database at all

        # cache MISS: the database decides (404 / 410 / redirect)
        async with self._session.begin():  # a short read-only transaction
            url = await self._urls.get_by_short_code(short_code)
            if url is None:
                raise UrlNotFoundError(short_code)
            if url.deleted_at is not None:
                raise UrlGoneError(short_code, "deleted")
            # Expiry before is_active: the nightly archive switches expired links
            # off, and "expired" is still the real reason for the visitor
            if url.is_expired(now):
                raise UrlGoneError(short_code, "expired")
            if not url.is_active:
                raise UrlGoneError(short_code, "inactive")
            entry = CachedRedirect(url.id, url.original_url, url.expires_at)

        await self._cache.set(short_code, entry, now)
        return entry
