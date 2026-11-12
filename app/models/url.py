from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    ColumnElement,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    and_,
    or_,
    true,
)
from sqlalchemy.ext.hybrid import hybrid_method
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.tag import url_tags

if TYPE_CHECKING:  # only for type checkers: avoids a circular import at runtime
    from app.models.click import Click
    from app.models.tag import Tag
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
    # Soft delete: the row stays (its short code is never reused, its clicks are kept)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Filled by the URL preview service in Module 9
    title: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    favicon: Mapped[str | None] = mapped_column(Text)

    click_count: Mapped[int] = mapped_column(default=0, server_default="0")

    owner: Mapped["User"] = relationship(back_populates="urls")
    clicks: Mapped[list["Click"]] = relationship(
        back_populates="url",
        cascade="all, delete-orphan",
        passive_deletes=True,  # let Postgres' ON DELETE CASCADE remove the clicks
    )
    # Many-to-many through the url_tags table. No delete-orphan: deleting a URL
    # must never delete the tags other URLs still use.
    tags: Mapped[list["Tag"]] = relationship(
        secondary=url_tags,
        back_populates="urls",
        passive_deletes=True,
    )

    # ---------- the "is this link usable?" rules, written once ----------
    # A hybrid method works in two places:
    #   url.is_live(now)  -> True/False, computed in Python on a loaded object
    #   URL.is_live(now)  -> a SQL condition, usable in select(URL).where(...)

    @hybrid_method
    def is_expired(self, now: datetime) -> bool:
        """Expiry reached. Exactly at expires_at counts as expired."""
        return self.expires_at is not None and self.expires_at <= now

    @is_expired.expression
    def is_expired(cls, now: datetime) -> ColumnElement[bool]:
        return cls.expires_at <= now  # NULL <= now is not true, so "no expiry" never matches

    @hybrid_method
    def is_live(self, now: datetime) -> bool:
        """Not deleted, switched on and not expired: the link may redirect."""
        return self.deleted_at is None and self.is_active and not self.is_expired(now)

    @is_live.expression
    def is_live(cls, now: datetime) -> ColumnElement[bool]:
        return and_(
            cls.deleted_at.is_(None),
            cls.is_active.is_(True),
            or_(cls.expires_at.is_(None), cls.expires_at > now),
        )

    def __repr__(self) -> str:
        return f"<URL id={self.id} short_code={self.short_code!r} owner_id={self.owner_id}>"
