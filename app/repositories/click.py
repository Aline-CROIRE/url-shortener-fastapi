from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Click


class ClickRepository:
    """All database access for clicks. Analytics queries arrive in F2.5."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def for_url(url_id: int) -> Select[tuple[Click]]:
        """Clicks of one URL, newest first (uses the (url_id, clicked_at) index)."""
        return (
            select(Click)
            .where(Click.url_id == url_id)
            .order_by(Click.clicked_at.desc(), Click.id.desc())
        )

    async def fetch_all(self, statement: Select[tuple[Click]]) -> list[Click]:
        return list(await self._session.scalars(statement))

    async def add(self, click: Click) -> Click:
        self._session.add(click)
        await self._session.flush()
        return click
