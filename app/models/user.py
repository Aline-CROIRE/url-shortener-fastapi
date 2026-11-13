"""The User model and the UserTier membership levels."""

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, Enum, String, false, true
from sqlalchemy.orm import Mapped, mapped_column, relationship, synonym, validates

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:  # only for type checkers: avoids a circular import at runtime
    from app.models.url import URL


class UserTier(StrEnum):
    """The fixed list of membership levels.

    Like Django choices: the VALUE is stored ("free"), the LABEL is shown ("Free").
    """

    FREE = "free"
    PREMIUM = "premium"
    ADMIN = "admin"

    @property
    def label(self) -> str:
        """Return the human-friendly name of the tier, e.g. "Free"."""
        return self.value.capitalize()  # "Free", "Premium", "Admin"

    @property
    def has_premium_features(self) -> bool:
        """Return True for the tiers that get premium features (premium and admin)."""
        return self is not UserTier.FREE


class User(TimestampMixin, Base):
    """The users table: a person who owns short URLs.

    Mirrors Django's AbstractUser (username, first_name, last_name, email,
    is_staff, is_active, is_superuser, last_login, date_joined) plus is_premium
    and tier. Login logic and password hashing arrive in Module 7.
    """

    __tablename__ = "users"
    __table_args__ = (
        # is_premium can never contradict tier, not even through raw SQL
        CheckConstraint(
            "is_premium = (tier IN ('premium', 'admin'))",
            name="is_premium_matches_tier",
        ),
    )

    email: Mapped[str] = mapped_column(String(255), unique=True)
    username: Mapped[str] = mapped_column(String(150), unique=True)  # Django's max length
    first_name: Mapped[str] = mapped_column(String(150), default="", server_default="")
    last_name: Mapped[str] = mapped_column(String(150), default="", server_default="")
    # Only the one-way hash is ever stored, never the real password (hashing: Module 7)
    hashed_password: Mapped[str] = mapped_column(String(255))
    # Account switches, as in AbstractUser (used by authentication in Module 7)
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    is_staff: Mapped[bool] = mapped_column(default=False, server_default=false())
    is_superuser: Mapped[bool] = mapped_column(default=False, server_default=false())
    last_login: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    is_premium: Mapped[bool] = mapped_column(default=False, server_default=false())
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

    # Django's name for the moment the account was created: the same column as created_at
    date_joined = synonym("created_at")

    urls: Mapped[list["URL"]] = relationship(
        back_populates="owner",
        lazy="raise",  # never loaded behind our back (see F3.1)
        # Deleting a user through the ORM also deletes their URLs...
        cascade="all, delete-orphan",
        # ...but let Postgres do it (ondelete="CASCADE"): one DELETE instead of
        # loading every URL into Python first
        passive_deletes=True,
    )

    @validates("tier")
    def _keep_is_premium_in_sync(self, key: str, tier: UserTier) -> UserTier:
        """Set is_premium whenever the tier is set (premium and admin get premium features)."""
        self.is_premium = UserTier(tier).has_premium_features
        return tier

    def __repr__(self) -> str:
        """Return a short text form of the user for logs and debugging."""
        return f"<User id={self.id} email={self.email!r} tier={self.tier}>"
