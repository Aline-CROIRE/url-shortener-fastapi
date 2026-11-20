"""Database access for users."""

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User


class UserRepository:
    """All database access for users. Never commits (see F2.2)."""

    def __init__(self, session: AsyncSession) -> None:
        """Keep the session used for every query."""
        self._session = session

    async def get(self, user_id: int) -> User | None:
        """Return the user with this id, or None."""
        return await self._session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        """Return the user with this email (any letter case), or None."""
        # Emails are stored lower-case (UserRegister lower-cases them), so a plain
        # comparison can use the uq_users_email index
        return await self._session.scalar(select(User).where(User.email == email.lower()))

    async def get_by_username(self, username: str) -> User | None:
        """Return the user with this username, or None."""
        return await self._session.scalar(select(User).where(User.username == username))

    async def add(self, user: User) -> User:
        """Add the user and flush so its id is filled in (no commit)."""
        self._session.add(user)
        await self._session.flush()  # sends the INSERT now: user.id is filled in
        return user

    async def record_login(self, user_id: int) -> None:
        """Set last_login to now, in one UPDATE.

        updated_at keeps its value: logging in is not editing the account. Without
        it, the onupdate=now() of TimestampMixin would change updated_at on every
        login (the same mistake as counting a click as an edit, fixed in Module 6).
        """
        await self._session.execute(
            update(User)
            .where(User.id == user_id)
            .values(last_login=func.now(), updated_at=User.updated_at)
            .execution_options(synchronize_session=False)
        )
