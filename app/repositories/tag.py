"""Database access for tags."""

from sqlalchemy import Select, String, column, func, select, values
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

        The names are inserted in sorted (lower-case) order. Postgres makes an
        insert wait while another transaction holds the same new, not yet
        committed name. If two requests inserted ["web", "python"] and
        ["python", "web"], each could wait for the other forever (a deadlock).
        With one fixed order the second request simply waits its turn.
        """
        wanted: dict[str, str] = {}  # lower-case name -> first spelling given
        for name in names:
            wanted.setdefault(name.lower(), name)
        if not wanted:
            return []

        await self._session.execute(
            insert(Tag)
            # same order for every request: no deadlock (see the docstring)
            .values([{"name": wanted[key]} for key in sorted(wanted)])
            .on_conflict_do_nothing()
        )
        # Let POSTGRES match each asked name to its tag, with the same lower() as the
        # unique index. Python's lower() can differ ("İ" -> "i" + a dot mark), and a
        # Python-side match would then miss the tag (a 500). Still one query.
        asked = values(column("name", String), name="asked").data(
            [(name,) for name in wanted.values()]
        )
        rows = await self._session.execute(
            select(asked.c.name, Tag).join(Tag, func.lower(Tag.name) == func.lower(asked.c.name))
        )
        # .all() first: a Result has .keys(), so dict(result) would treat it as a mapping
        tag_for = dict(rows.tuples().all())
        tags = [tag_for[name] for name in wanted.values()]  # same order as asked
        # Two names Python sees as different can still be ONE tag for Postgres:
        # keep each tag once, or the url_tags rows would clash
        return list({tag.id: tag for tag in tags}.values())
