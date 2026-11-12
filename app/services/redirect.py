from dataclasses import dataclass
from datetime import datetime
from ipaddress import IPv4Address, IPv6Address

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import CachedRedirect, NullRedirectCache, RedirectCache
from app.core.exceptions import UrlGoneError, UrlNotFoundError
from app.models import Click
from app.repositories import ClickRepository, URLRepository
from app.repositories.url import utc_now

FOREIGN_KEY_VIOLATION = "23503"  # PostgreSQL SQLSTATE: the referenced row does not exist


@dataclass(frozen=True)
class VisitorInfo:
    """What we record about one visit (no HTTP objects: the service stays testable)."""

    ip_address: IPv4Address | IPv6Address | None = None
    user_agent: str | None = None
    referrer: str | None = None


class RedirectService:
    """Handles one visit of a short link.

    TRANSACTION RULE (the one place that commits): the service method opens the
    transaction with "async with session.begin()". Leaving the block normally
    COMMITS; an exception anywhere inside ROLLS BACK everything. Repositories only
    flush, routes never commit.

    CACHE-ASIDE: look in the cache first; on a hit skip the SELECT (the click is
    still recorded); on a miss read the database and fill the cache.
    """

    def __init__(
        self,
        session: AsyncSession,
        urls: URLRepository,
        clicks: ClickRepository,
        cache: RedirectCache | None = None,
    ) -> None:
        self._session = session
        self._urls = urls
        self._clicks = clicks
        self._cache = cache or NullRedirectCache()

    async def visit(self, short_code: str, visitor: VisitorInfo, now: datetime | None = None) -> str:
        """Record the click and return the original URL to redirect to.

        Raises UrlNotFoundError (404) or UrlGoneError (410).
        """
        now = now or utc_now()

        cached = await self._cache.get(short_code)
        if cached is not None and cached.is_expired(now):
            await self._cache.invalidate(short_code)  # expired while cached: ask the DB
            cached = None
        if cached is not None:
            target = await self._visit_cached(short_code, cached, visitor)
            if target is not None:
                return target  # cache HIT: no SELECT was needed

        # cache MISS: the database decides (404 / 410 / redirect)
        async with self._session.begin():  # BEGIN ... COMMIT (or ROLLBACK on error)
            url = await self._urls.get_by_short_code(short_code)
            if url is None:
                raise UrlNotFoundError(short_code)
            if url.deleted_at is not None:
                raise UrlGoneError(short_code, "deleted")
            if not url.is_active:
                raise UrlGoneError(short_code, "inactive")
            if url.is_expired(now):
                raise UrlGoneError(short_code, "expired")
            await self._record_click(url.id, visitor)
            entry = CachedRedirect(url.id, url.original_url, url.expires_at)

        # Fill the cache only AFTER the commit succeeded
        await self._cache.set(short_code, entry, now)
        return entry.original_url

    async def _visit_cached(
        self, short_code: str, cached: CachedRedirect, visitor: VisitorInfo
    ) -> str | None:
        """Record the click for a cached link; None if the cache was stale."""
        try:
            async with self._session.begin():
                await self._record_click(cached.url_id, visitor)
            return cached.original_url
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) != FOREIGN_KEY_VIOLATION:
                raise
            # The URL row no longer exists (e.g. its owner was deleted): the cached
            # entry is stale. Forget it and let the database answer (404).
            await self._cache.invalidate(short_code)
            return None

    async def _record_click(self, url_id: int, visitor: VisitorInfo) -> None:
        # Write 1: a new row in the visitor's book. url_id (not url=url): setting
        # the relationship would also touch url.clicks, which is never loaded.
        await self._clicks.add(
            Click(
                url_id=url_id,
                ip_address=visitor.ip_address,
                user_agent=visitor.user_agent,
                referrer=visitor.referrer,
            )
        )
        # Write 2: +1 computed by Postgres (no lost update)
        await self._urls.increment_click_count(url_id)
