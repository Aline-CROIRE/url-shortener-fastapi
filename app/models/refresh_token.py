"""The RefreshToken model: one row per refresh token the server has handed out.

Rotation: a refresh token works ONCE (used_at is set when it is swapped).
Reuse detection: if a token whose used_at is already set comes back, it was
copied (stolen), so every refresh token of that user is deleted.
Only the token's id (jti) is stored, never the token: a leak of this table
gives an attacker nothing that can be sent to the API.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# secrets.token_urlsafe(16) is 22 characters; room to spare
JTI_MAX_LENGTH = 64


class RefreshToken(Base):
    """The refresh_tokens table."""

    __tablename__ = "refresh_tokens"

    # The token's own unique id: the primary key, so "find this token" is one lookup
    jti: Mapped[str] = mapped_column(String(JTI_MAX_LENGTH), primary_key=True)
    # Deleting a user deletes their refresh tokens (no token outlives its account).
    # Indexed: "revoke every token of this user" and the cleanup at login use it.
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # NULL = not used yet; set (once) when the token is swapped for a new pair
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        """Return a short text form for logs and debugging (no secrets in it)."""
        return f"<RefreshToken user_id={self.user_id} used={self.used_at is not None}>"
