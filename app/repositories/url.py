from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import Select, exists, func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.models import URL, Tag

MAX_LIMIT = 100

# Where the previous page ended: (value of the sort column, id)
CursorPosition = tuple[datetime | int, int]


def utc_now() -> datetime:
    return datetime.now(UTC)


class URLSort(StrEnum):
    NEWEST = "newest"  # created_at DESC, id DESC
    MOST_CLICKED = "most_clicked"  # click_count DESC, id DESC


@dataclass(frozen=True)
class URLFilters:
    """Optional filters for the URL list. None = "not asked for" (no condition added)."""

    tag: str | None = None
    is_active: bool | None = None
    expired: bool | None = None
    search: str | None = None


def escape_like(text: str) -> str:
    """Make %, _ and \\ in user input match literally inside LIKE / ILIKE."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def sort_column(sort: URLSort):  # noqa: ANN201 (a SQLAlchemy column)
    return URL.created_at if sort is URLSort.NEWEST else URL.click_count


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

    @staticmethod
    def list_owned(
        owner_id: int,
        filters: URLFilters,
        sort: URLSort = URLSort.NEWEST,
        after: CursorPosition | None = None,
        now: datetime | None = None,
    ) -> Select[tuple[URL]]:
        """One owner's (not deleted) URLs, filtered, sorted, starting after a bookmark.

        The caller adds .limit(): keyset pagination never uses OFFSET.
        """
        now = now or utc_now()
        statement = select(URL).where(URL.owner_id == owner_id, URL.deleted_at.is_(None))

        # Each filter is added ONLY when the client asked for it
        if filters.tag is not None:
            # .any() = EXISTS (SELECT ... FROM url_tags JOIN tags ...): never duplicates
            # a URL, unlike a JOIN would
            statement = statement.where(URL.tags.any(func.lower(Tag.name) == filters.tag.lower()))
        if filters.is_active is not None:
            statement = statement.where(URL.is_active.is_(filters.is_active))
        if filters.expired is True:
            statement = statement.where(URL.is_expired(now))
        elif filters.expired is False:
            # Written out on purpose: NOT (expires_at <= now) is NULL, not true, when
            # expires_at is NULL, so "never expires" URLs would silently disappear
            statement = statement.where(or_(URL.expires_at.is_(None), URL.expires_at > now))
        if filters.search is not None:
            pattern = f"%{escape_like(filters.search)}%"
            statement = statement.where(
                or_(
                    URL.original_url.ilike(pattern, escape="\\"),
                    URL.title.ilike(pattern, escape="\\"),
                )
            )

        # Keyset: rows that come AFTER the bookmark in (sort value DESC, id DESC) order.
        # (a, b) < (x, y) compares a first, and b only when a is equal (the tie-breaker)
        column = sort_column(sort)
        if after is not None:
            after_value, after_id = after
            statement = statement.where(tuple_(column, URL.id) < tuple_(after_value, after_id))

        return statement.order_by(column.desc(), URL.id.desc()).options(
            joinedload(URL.owner), selectinload(URL.tags)  # needed by URLRead (see F3.1)
        )

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

    async def get_owned(self, short_code: str, owner_id: int) -> URL | None:
        """One NOT deleted URL of this owner, with owner and tags already loaded
        (async code cannot lazy-load them later). Someone else's URL -> None.
        """
        return await self._session.scalar(
            select(URL)
            .where(
                URL.short_code == short_code,
                URL.owner_id == owner_id,
                URL.deleted_at.is_(None),
            )
            .options(joinedload(URL.owner), selectinload(URL.tags))
        )

    async def owned_url_id(self, short_code: str, owner_id: int) -> int | None:
        """Just the id of one NOT deleted URL of this owner (nothing else is loaded)."""
        return await self._session.scalar(
            select(URL.id).where(
                URL.short_code == short_code,
                URL.owner_id == owner_id,
                URL.deleted_at.is_(None),
            )
        )

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
