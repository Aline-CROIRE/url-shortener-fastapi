"""The URL service: create, list, read, update and delete short URLs, with ownership checks."""

from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import NullRedirectCache, RedirectCache
from app.core.exceptions import (
    AliasTakenError,
    ForbiddenError,
    FreeTierLimitError,
    InsufficientTierError,
    InvalidCursorError,
    InvalidTokenError,
    RedirectLoopError,
    ShortCodeGenerationError,
    UrlNotFoundError,
)
from app.db.errors import unique_violation_constraint
from app.models import URL, User, UserTier
from app.repositories import TagRepository, URLRepository, UserRepository
from app.repositories.url import URLFilters, URLSort, utc_now
from app.schemas.url import URLCreate, URLUpdate
from app.services.pagination import decode_cursor, encode_cursor
from app.utils.codes import generate_code

MAX_CODE_ATTEMPTS = 5
# A Free account may have at most this many LIVE URLs (switched on, not deleted,
# not expired). Premium and Admin accounts have no limit.
FREE_URL_LIMIT = 10
ALIAS_NEEDS_PREMIUM = (
    "Custom aliases are a Premium feature. Leave custom_alias empty to get a random "
    "code, or upgrade to Premium."
)
# The two unique constraints a new short code can clash with
SHORT_CODE_CONSTRAINTS = {"ix_urls_short_code", "uq_urls_custom_alias"}


class URLService:
    """Create, read, update and (soft) delete the current owner's URLs.

    TRANSACTION RULE: every method that writes opens ONE transaction with
    "async with session.begin()": commit on success, rollback on any error.
    """

    def __init__(
        self,
        session: AsyncSession,
        urls: URLRepository,
        users: UserRepository,
        tags: TagRepository,
        cache: RedirectCache | None = None,
        base_url: str | None = None,
    ) -> None:
        """Keep the session, repositories and cache (no cache -> a cache that stores nothing)."""
        # base_url is this shortener's own address (the BASE_URL setting): original
        # URLs on the same host are refused, because they would loop
        self._session = session
        self._urls = urls
        self._users = users
        self._tags = tags
        self._cache = cache or NullRedirectCache()
        self._own_host = urlsplit(base_url).hostname if base_url else None

    def _refuse_own_links(self, original_url: str) -> None:
        """Refuse an original URL that points back to this shortener.

        Example: abc1234 -> http://our-host/abc1234 would redirect to itself forever
        (and count a click each time); two links pointing at each other loop too.
        Only the host is compared (hostname is lower-cased), so another scheme or
        port on our host is refused as well.

        Raises:
            RedirectLoopError: If the original URL's host is our own host.
        """
        if self._own_host is not None and urlsplit(original_url).hostname == self._own_host:
            raise RedirectLoopError(original_url)

    @staticmethod
    def _check_access(url: URL | None, short_code: str, actor_id: int, is_admin: bool) -> URL:
        """Return the URL if the caller may use it: the owner, or an Admin.

        Raises:
            UrlNotFoundError: No such URL, or it is deleted (404): nothing to protect.
            ForbiddenError: The URL exists but belongs to someone else (403).
        """
        if url is None:
            raise UrlNotFoundError(short_code)
        if url.owner_id != actor_id and not is_admin:
            raise ForbiddenError()
        return url

    async def _lock_owner(self, owner_id: int) -> User:
        """Lock the owner's row (SELECT ... FOR UPDATE) and return it, fresh.

        Every write that can add a LIVE URL takes this lock FIRST, so for one user
        "count the live URLs, then add one" runs one request at a time, and the
        lock order is always the same: user row, then URL row, then tags (sorted).

        Raises:
            InvalidTokenError: The user was deleted after their token was checked.
        """
        owner = await self._users.get_for_update(owner_id)
        if owner is None:
            raise InvalidTokenError()
        return owner

    async def _check_free_limit(self, owner: User) -> None:
        """Refuse one more live URL for a Free owner who already has FREE_URL_LIMIT.

        Must run while the owner's row is locked (see _lock_owner), in the same
        transaction as the change, or two requests could both see 9 and both add one.

        Raises:
            FreeTierLimitError: The Free owner already has FREE_URL_LIMIT live URLs.
        """
        if owner.tier != UserTier.FREE:
            return
        if await self._urls.count_live(owner.id) >= FREE_URL_LIMIT:
            raise FreeTierLimitError(FREE_URL_LIMIT)

    async def create(self, data: URLCreate, owner_id: int) -> URL:
        """Save the URL and its tags together.

        A chosen alias becomes the short code. No "does this code exist?" check
        first: the unique constraint is the real guard (also when two requests
        race), so we try and react to its error.

        The tier rules use the owner's row read under a lock (never the token):
        a Free owner may not choose an alias and may have at most FREE_URL_LIMIT
        live URLs.

        Raises:
            InsufficientTierError: A Free owner sent a custom alias (403).
            FreeTierLimitError: A Free owner already has FREE_URL_LIMIT live URLs (403).
            AliasTakenError: If the chosen alias is already used.
            RedirectLoopError: If the original URL points back to this shortener.
            ShortCodeGenerationError: If no free random code was found after
                MAX_CODE_ATTEMPTS tries.
        """
        self._refuse_own_links(str(data.original_url))
        attempts = 1 if data.custom_alias else MAX_CODE_ATTEMPTS
        for _ in range(attempts):
            short_code = data.custom_alias or generate_code()
            try:
                async with self._session.begin():
                    owner = await self._lock_owner(owner_id)  # waits for the user's other writes
                    if data.custom_alias and not owner.is_premium:
                        raise InsufficientTierError(ALIAS_NEEDS_PREMIUM)
                    await self._check_free_limit(owner)
                    url = URL(
                        original_url=str(data.original_url),
                        short_code=short_code,
                        custom_alias=data.custom_alias,
                        expires_at=data.expires_at,
                        owner=owner,
                        tags=await self._tags.get_or_create_many(data.tags),
                    )
                    await self._urls.add(url)  # INSERT url + url_tags rows
                return url  # the block ended normally: COMMITTED
            except IntegrityError as exc:  # the block already ROLLED BACK
                if unique_violation_constraint(exc) not in SHORT_CODE_CONSTRAINTS:
                    raise
                if data.custom_alias:
                    raise AliasTakenError(data.custom_alias) from exc
                # A random code clashed (very rare): try again with a new one
        raise ShortCodeGenerationError()

    async def list_page(
        self,
        owner_id: int,
        filters: URLFilters,
        sort: URLSort,
        limit: int,
        cursor: str | None = None,
    ) -> tuple[list[URL], str | None]:
        """Return one page of the owner's URLs and the cursor of the next page (or None).

        "newest" has pages. "most_clicked" is ONE "top N" page (next cursor always
        None): click counts change on every visit, so a bookmark on them would skip
        or repeat URLs that moved between two requests.

        Raises:
            InvalidCursorError: If the cursor text is broken, or sent with most_clicked.
        """
        if sort is URLSort.MOST_CLICKED:
            if cursor:
                raise InvalidCursorError(
                    "most_clicked is a top list with one page: send it without a cursor."
                )
            statement = self._urls.list_owned(owner_id, filters, sort)
            return await self._urls.fetch_all(statement.limit(limit)), None

        after = decode_cursor(cursor) if cursor else None
        statement = self._urls.list_owned(owner_id, filters, sort, after)
        # Ask for ONE extra row: if it exists, there is a next page (no COUNT(*) needed)
        rows = await self._urls.fetch_all(statement.limit(limit + 1))
        page = rows[:limit]
        if len(rows) <= limit:
            return page, None
        last = page[-1]
        return page, encode_cursor((last.created_at, last.id))

    async def get(self, short_code: str, actor_id: int, *, is_admin: bool = False) -> URL:
        """Return one URL to its owner (or to an Admin).

        Read only: no explicit transaction needed (get_db closes the session).

        Raises:
            UrlNotFoundError: If the URL does not exist or is deleted.
            ForbiddenError: If it is someone else's and the caller is not an Admin.
        """
        url = await self._urls.get_visible(short_code)
        return self._check_access(url, short_code, actor_id, is_admin)

    @staticmethod
    def _comes_back_to_life(url: URL, changes: dict[str, Any]) -> bool:
        """Return True if the changes turn a URL that is not live into a live one.

        Editing a URL that is already live (or that stays not live) adds no live
        URL, so it never counts against the Free limit.
        """
        now = utc_now()
        if url.is_live(now):
            return False
        is_active = changes.get("is_active", url.is_active)
        expires_at = changes.get("expires_at", url.expires_at)
        not_expired = expires_at is None or expires_at > now
        return url.deleted_at is None and bool(is_active) and not_expired

    async def update(
        self, short_code: str, actor_id: int, data: URLUpdate, *, is_admin: bool = False
    ) -> URL:
        """Change only the fields the client sent (partial update).

        Raises:
            UrlNotFoundError: If the URL does not exist or is deleted.
            ForbiddenError: If it is someone else's and the caller is not an Admin.
            FreeTierLimitError: The change would give a Free owner more than
                FREE_URL_LIMIT live URLs (switching one back on, or giving an
                expired one a new date). An Admin cannot go past it either: the
                limit belongs to the owner's account.
            RedirectLoopError: If the new original URL points back to this shortener.
        """
        changes = data.model_dump(exclude_unset=True)
        if "original_url" in changes:
            changes["original_url"] = str(changes["original_url"])
            self._refuse_own_links(changes["original_url"])  # before any database work
        # Only these two fields can bring a URL back to life
        may_revive = changes.get("is_active") is True or "expires_at" in changes
        async with self._session.begin():
            owner: User | None = None
            if may_revive:
                # Lock order: the OWNER's row first (as in create), then the URL row,
                # then tags (sorted). Redirects lock only the URL row: no lock cycle.
                owner_id = await self._urls.owner_id_of(short_code)  # never changes
                if owner_id is None:
                    raise UrlNotFoundError(short_code)
                if owner_id != actor_id and not is_admin:
                    raise ForbiddenError()  # before locking someone else's row
                owner = await self._lock_owner(owner_id)
            found = await self._urls.get_visible(short_code, for_update=True)
            url = self._check_access(found, short_code, actor_id, is_admin)
            if owner is not None and self._comes_back_to_life(url, changes):
                await self._check_free_limit(owner)

            new_tags = changes.pop("tags", None)
            for field, value in changes.items():
                setattr(url, field, value)
            if new_tags is not None:
                url.tags = await self._tags.get_or_create_many(new_tags)  # replaces all tags
            if changes or new_tags is not None:
                # Also when only the tags changed: the URL did change
                url.updated_at = func.now()
            await self._session.flush()
        # AFTER the commit: invalidating earlier would let another request re-cache
        # the old version before our change is saved
        await self._cache.invalidate(short_code)
        return url

    async def delete(self, short_code: str, actor_id: int, *, is_admin: bool = False) -> None:
        """Soft-delete the URL (by its owner or an Admin).

        The row stays, so the code is never reused and clicks are kept.

        Raises:
            UrlNotFoundError: If the URL does not exist or is already deleted.
            ForbiddenError: If it is someone else's and the caller is not an Admin.
        """
        async with self._session.begin():
            found = await self._urls.get_visible(short_code)
            url = self._check_access(found, short_code, actor_id, is_admin)
            url.deleted_at = func.now()
        await self._cache.invalidate(short_code)  # after the commit (see update)
