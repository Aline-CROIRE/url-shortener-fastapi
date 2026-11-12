from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:  # only for type checkers: avoids a circular import at runtime
    from app.models.user import User

SHORT_CODE_MAX_LENGTH = 10


class URL(TimestampMixin, Base):
    """A short link owned by a user."""

    __tablename__ = "urls"
    __table_args__ = (
        CheckConstraint("click_count >= 0", name="click_count_non_negative"),
        # A chosen alias IS the short code (one shared space for all codes)
        CheckConstraint(
            "custom_alias IS NULL OR custom_alias = short_code",
            name="custom_alias_matches_short_code",
        ),
        Index("ix_urls_created_at", "created_at"),  # fast "newest first" lists
    )

    original_url: Mapped[str] = mapped_column(Text)
    short_code: Mapped[str] = mapped_column(
        String(SHORT_CODE_MAX_LENGTH), unique=True, index=True
    )
    custom_alias: Mapped[str | None] = mapped_column(
        String(SHORT_CODE_MAX_LENGTH), unique=True
    )
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,  # Postgres does not index foreign keys automatically
    )
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Filled by the URL preview service in Module 9
    title: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    favicon: Mapped[str | None] = mapped_column(Text)

    click_count: Mapped[int] = mapped_column(default=0, server_default="0")

    owner: Mapped["User"] = relationship(back_populates="urls")

    def __repr__(self) -> str:
        return f"<URL id={self.id} short_code={self.short_code!r} owner_id={self.owner_id}>"
