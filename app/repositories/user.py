from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User


class UserRepository:
    """All database access for users. Never commits (see F2.2)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: int) -> User | None:
        return await self._session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        # Emails are stored lower-case (UserCreate lower-cases them), so a plain
        # comparison can use the uq_users_email index
        return await self._session.scalar(select(User).where(User.email == email.lower()))

    async def get_by_username(self, username: str) -> User | None:
        return await self._session.scalar(select(User).where(User.username == username))

    async def add(self, user: User) -> User:
        self._session.add(user)
        await self._session.flush()  # sends the INSERT now: user.id is filled in
        return user
