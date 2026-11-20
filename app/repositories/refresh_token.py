"""Database access for refresh tokens (rotation and reuse detection)."""

from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import RefreshToken


class RefreshTokenRepository:
    """All database access for refresh tokens. Never commits (see F2.2)."""

    def __init__(self, session: AsyncSession) -> None:
        """Keep the session used for every query."""
        self._session = session

    async def add(self, jti: str, user_id: int, expires_at: datetime) -> None:
        """Remember a refresh token that was just handed out (by its id only)."""
        self._session.add(RefreshToken(jti=jti, user_id=user_id, expires_at=expires_at))
        await self._session.flush()

    async def use(self, jti: str, user_id: int) -> bool:
        """Mark the token as used; return True only for the FIRST use of a valid token.

        ONE atomic statement: UPDATE ... SET used_at = now() WHERE it is unused and
        not expired. Two requests with the same token at the same moment cannot
        both get True: Postgres lets only one of them change the row.
        """
        result = await self._session.execute(
            update(RefreshToken)
            .where(
                RefreshToken.jti == jti,
                RefreshToken.user_id == user_id,
                RefreshToken.used_at.is_(None),
                RefreshToken.expires_at > func.now(),
            )
            .values(used_at=func.now())
            .returning(RefreshToken.jti)
        )
        return result.scalar_one_or_none() is not None

    async def was_used(self, jti: str, user_id: int) -> bool:
        """Return True if this token was handed out AND already used (a replay)."""
        used_at = await self._session.scalar(
            select(RefreshToken.used_at).where(
                RefreshToken.jti == jti, RefreshToken.user_id == user_id
            )
        )
        return used_at is not None

    async def revoke_all(self, user_id: int) -> None:
        """Delete every refresh token of the user: all of them stop working at once."""
        await self._session.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))

    async def delete_expired(self, user_id: int) -> None:
        """Remove the user's expired tokens (housekeeping at login: the table stays small)."""
        await self._session.execute(
            delete(RefreshToken).where(
                RefreshToken.user_id == user_id, RefreshToken.expires_at <= func.now()
            )
        )
