"""Database access for tags."""

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Tag


class TagRepository:
    """All database access for tags. Tag names are unique ignoring case."""

    def __init__(self, session: AsyncSession) -> None:
        """Keep the session used for every query."""
        self._session = session

    @staticmethod
    def all_tags() -> Select[tuple[Tag]]:
        """Return a statement for all tags, sorted by name."""
        return select(Tag).order_by(Tag.name)

    async def fetch_all(self, statement: Select[tuple[Tag]]) -> list[Tag]:
        """Run the statement and return every tag it finds."""
        return list(await self._session.scalars(statement))

    async def get_by_name(self, name: str) -> Tag | None:
        """Return the tag with this name (any letter case), or None."""
        # lower(name) = ... matches the uq_tags_name_lower index exactly
        return await self._session.scalar(select(Tag).where(func.lower(Tag.name) == name.lower()))

    async def get_or_create_many(self, names: list[str]) -> list[Tag]:
        """Return a Tag for every name, creating the missing ones.

        INSERT ... ON CONFLICT DO NOTHING skips names that already exist (in any
        letter case), even if another request creates the same tag at the same
        moment. Existing tags keep their original spelling.
        """
        wanted: dict[str, str] = {}  # lower-case name -> first spelling given
        for name in names:
            wanted.setdefault(name.lower(), name)
        if not wanted:
            return []

        await self._session.execute(
            insert(Tag)
            .values([{"name": name} for name in wanted.values()])
            .on_conflict_do_nothing()
        )
        found = await self._session.scalars(
            select(Tag).where(func.lower(Tag.name).in_(wanted.keys()))
        )
        by_lower_name = {tag.name.lower(): tag for tag in found}
        return [by_lower_name[key] for key in wanted]  # same order as asked
