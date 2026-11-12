from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AliasTakenError, ShortCodeGenerationError, UrlNotFoundError
from app.db.errors import unique_violation_constraint
from app.models import URL
from app.repositories import TagRepository, URLRepository, UserRepository
from app.schemas.url import URLCreate, URLUpdate
from app.utils.codes import generate_code

MAX_CODE_ATTEMPTS = 5
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
    ) -> None:
        self._session = session
        self._urls = urls
        self._users = users
        self._tags = tags

    async def create(self, data: URLCreate, owner_id: int) -> URL:
        """Save the URL and its tags together. A chosen alias becomes the short code.

        No "does this code exist?" check first: the unique constraint is the real
        guard (also when two requests race), so we try and react to its error.
        """
        attempts = 1 if data.custom_alias else MAX_CODE_ATTEMPTS
        for _ in range(attempts):
            short_code = data.custom_alias or generate_code()
            try:
                async with self._session.begin():
                    url = URL(
                        original_url=str(data.original_url),
                        short_code=short_code,
                        custom_alias=data.custom_alias,
                        expires_at=data.expires_at,
                        owner=await self._users.get(owner_id),
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

    async def get(self, short_code: str, owner_id: int) -> URL:
        """Read only: no explicit transaction needed (get_db closes the session)."""
        url = await self._urls.get_owned(short_code, owner_id)
        if url is None:
            raise UrlNotFoundError(short_code)
        return url

    async def update(self, short_code: str, owner_id: int, data: URLUpdate) -> URL:
        """Change only the fields the client sent (partial update)."""
        changes = data.model_dump(exclude_unset=True)
        async with self._session.begin():
            url = await self._urls.get_owned(short_code, owner_id)
            if url is None:
                raise UrlNotFoundError(short_code)

            new_tags = changes.pop("tags", None)
            if "original_url" in changes:
                changes["original_url"] = str(changes["original_url"])
            for field, value in changes.items():
                setattr(url, field, value)
            if new_tags is not None:
                url.tags = await self._tags.get_or_create_many(new_tags)  # replaces all tags
            if changes or new_tags is not None:
                # Also when only the tags changed: the URL did change
                url.updated_at = func.now()
            await self._session.flush()
        return url

    async def delete(self, short_code: str, owner_id: int) -> None:
        """Soft delete: the row stays, so the code is never reused and clicks are kept."""
        async with self._session.begin():
            url = await self._urls.get_owned(short_code, owner_id)
            if url is None:
                raise UrlNotFoundError(short_code)
            url.deleted_at = func.now()
