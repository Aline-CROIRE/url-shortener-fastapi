from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Enum, String, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:  # only for type checkers: avoids a circular import at runtime
    from app.models.url import URL


class UserTier(StrEnum):
    """The fixed list of membership levels."""

    FREE = "free"
    PREMIUM = "premium"
    ADMIN = "admin"


class User(TimestampMixin, Base):
    """A person who owns short URLs. Login logic arrives in Module 7."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True)
    username: Mapped[str] = mapped_column(String(50), unique=True)
    # Only the one-way hash is ever stored, never the real password (hashing: Module 7)
    hashed_password: Mapped[str] = mapped_column(String(255))
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

    urls: Mapped[list["URL"]] = relationship(
        back_populates="owner",
        lazy="raise",  # never loaded behind our back (see F3.1)
        # Deleting a user through the ORM also deletes their URLs...
        cascade="all, delete-orphan",
        # ...but let Postgres do it (ondelete="CASCADE"): one DELETE instead of
        # loading every URL into Python first
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r} tier={self.tier}>"
