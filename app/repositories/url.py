from datetime import UTC, datetime

from sqlalchemy import Select, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import URL

MAX_LIMIT = 100


def utc_now() -> datetime:
    return datetime.now(UTC)


class URLRepository:
    """All database access for short URLs (like a Django Manager: URL.objects).

    The *_urls() methods return a select() statement, not results, so callers
    can keep adding .where(), .order_by() or .limit() before running it once.
    Never commits (see F2.2).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---------- composable statements (nothing is sent to the database yet) ----------

    @staticmethod
    def active_urls(now: datetime | None = None) -> Select[tuple[URL]]:
        """Switched on AND (no expiry OR expiry still in the future)."""
        now = now or utc_now()
        return select(URL).where(
            URL.is_active.is_(True),
            or_(URL.expires_at.is_(None), URL.expires_at > now),
        )

    @staticmethod
    def expired_urls(now: datetime | None = None) -> Select[tuple[URL]]:
        """Expiry reached. "<=" (not "<"): a URL expiring exactly now is expired,
        so no URL falls between active_urls() and expired_urls().
        """
        now = now or utc_now()
        return select(URL).where(URL.expires_at <= now)

    @staticmethod
    def popular_urls(limit: int = 10) -> Select[tuple[URL]]:
        """Most clicked first; id breaks ties so the order is always the same."""
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
        return select(URL).order_by(URL.click_count.desc(), URL.id).limit(limit)

    # ---------- running statements ----------

    async def fetch_all(self, statement: Select[tuple[URL]]) -> list[URL]:
        return list(await self._session.scalars(statement))

    async def fetch_first(self, statement: Select[tuple[URL]]) -> URL | None:
        return (await self._session.scalars(statement)).first()

    # ---------- single URLs ----------

    async def get(self, url_id: int) -> URL | None:
        return await self._session.get(URL, url_id)

    async def get_by_short_code(self, short_code: str) -> URL | None:
        return await self._session.scalar(select(URL).where(URL.short_code == short_code))

    async def short_code_exists(self, short_code: str) -> bool:
        # SELECT EXISTS(...): Postgres stops at the first match, no row is loaded
        return bool(
            await self._session.scalar(select(exists().where(URL.short_code == short_code)))
        )

    async def add(self, url: URL) -> URL:
        self._session.add(url)
        await self._session.flush()
        return url
