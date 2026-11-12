from datetime import UTC, datetime

from sqlalchemy import Select, exists, select, update
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
        return select(URL).where(URL.is_live(now or utc_now()))

    @staticmethod
    def expired_urls(now: datetime | None = None) -> Select[tuple[URL]]:
        """Expiry reached. "<=" (not "<"): a URL expiring exactly now is expired,
        so no URL falls between active_urls() and expired_urls().
        """
        return select(URL).where(URL.is_expired(now or utc_now()))

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

    async def increment_click_count(self, url_id: int) -> None:
        """Add 1 INSIDE the database: UPDATE urls SET click_count = click_count + 1.

        Postgres reads and writes the value in one step under a row lock, so two
        visits at the same moment both count (no "lost update"). Reading the
        number into Python, adding 1 and writing it back would lose one of them.
        """
        await self._session.execute(
            update(URL)
            .where(URL.id == url_id)
            .values(click_count=URL.click_count + 1)
            # the URL object in this session may be stale now; don't try to
            # recompute it in Python, just leave it for a fresh read if needed
            .execution_options(synchronize_session=False)
        )
