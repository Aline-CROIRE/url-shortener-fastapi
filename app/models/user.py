"""The User model and the UserTier membership levels."""

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, String, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:  # only for type checkers: avoids a circular import at runtime
    from app.models.url import URL


class UserTier(StrEnum):
    """The fixed list of membership levels; the value ("free") is stored."""

    FREE = "free"
    PREMIUM = "premium"
    ADMIN = "admin"

    @property
    def has_premium_features(self) -> bool:
        """Return True for the tiers that get premium features (premium and admin)."""
        return self is not UserTier.FREE


class User(TimestampMixin, Base):
    """The users table: a person who owns short URLs.

    The tier decides what a user may do: free, premium or admin (one field, so
    there is no second "is admin" flag that could disagree with it).
    is_premium is NOT a column: it is worked out from tier (one source of truth).
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True)
    username: Mapped[str] = mapped_column(String(150), unique=True)
    # Only the one-way hash is ever stored, never the real password
    hashed_password: Mapped[str] = mapped_column(String(255))
    # False = the account is switched off and cannot log in
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    tier: Mapped[UserTier] = mapped_column(
        Enum(
            UserTier,
            name="user_tier",  # name of the Postgres ENUM type
            # store the values ("free") instead of the Python names ("FREE")
            values_callable=lambda tiers: [tier.value for tier in tiers],
        ),
        default=UserTier.FREE,
        server_default=UserTier.FREE.value,
    )

    urls: Mapped[list["URL"]] = relationship(
        back_populates="owner",
        lazy="raise",  # never loaded behind our back (see F3.1)
        # Deleting a user through the ORM also deletes their URLs...
        cascade="all, delete-orphan",
        # ...but let Postgres do it (ondelete="CASCADE"): one DELETE instead of
        # loading every URL into Python first
        passive_deletes=True,
    )

    @property
    def is_premium(self) -> bool:
        """Return True for the tiers with premium features (premium and admin).

        Read-only and computed from tier, so the two can never disagree.
        Before the first save tier may still be None (the default is applied
        on INSERT): that counts as free.
        """
        return self.tier is not None and UserTier(self.tier).has_premium_features

    def __repr__(self) -> str:
        """Return a short text form of the user for logs and debugging."""
        return f"<User id={self.id} email={self.email!r} tier={self.tier}>"
