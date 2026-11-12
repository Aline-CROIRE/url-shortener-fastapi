from dataclasses import dataclass
from datetime import datetime
from ipaddress import IPv4Address, IPv6Address

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import UrlGoneError, UrlNotFoundError
from app.models import Click
from app.repositories import ClickRepository, URLRepository
from app.repositories.url import utc_now


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
    """

    def __init__(self, session: AsyncSession, urls: URLRepository, clicks: ClickRepository) -> None:
        self._session = session
        self._urls = urls
        self._clicks = clicks

    async def visit(self, short_code: str, visitor: VisitorInfo, now: datetime | None = None) -> str:
        """Record the click and return the original URL to redirect to.

        Raises UrlNotFoundError (404) or UrlGoneError (410).
        """
        now = now or utc_now()
        async with self._session.begin():  # BEGIN ... COMMIT (or ROLLBACK on error)
            url = await self._urls.get_by_short_code(short_code)
            if url is None:
                raise UrlNotFoundError(short_code)
            if not url.is_active:
                raise UrlGoneError(short_code, "inactive")
            if url.is_expired(now):
                raise UrlGoneError(short_code, "expired")

            # Write 1: a new row in the visitor's book. url_id (not url=url): setting
            # the relationship would also append to url.clicks, which is not loaded
            # and cannot be lazy-loaded in async code.
            await self._clicks.add(
                Click(
                    url_id=url.id,
                    ip_address=visitor.ip_address,
                    user_agent=visitor.user_agent,
                    referrer=visitor.referrer,
                )
            )
            # Write 2: +1 computed by Postgres (no lost update)
            await self._urls.increment_click_count(url.id)
            return url.original_url
