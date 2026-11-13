"""The URL model and its manager (URL.objects) with ready-made queries."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from sqlalchemy import (
    CheckConstraint,
    ColumnElement,
    DateTime,
    ForeignKey,
    Index,
    Select,
    String,
    Text,
    and_,
    or_,
    select,
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
MAX_POPULAR_LIMIT = 100


class URL(TimestampMixin, Base):
    """The urls table: a short link owned by a user.

    URL.objects is the Django-style manager: URL.objects.active_urls() etc.
    """

    # ClassVar: a plain class attribute, not a database column
    objects: ClassVar["URLManager"]

    __tablename__ = "urls"
    __table_args__ = (
        CheckConstraint("click_count >= 0", name="click_count_non_negative"),
        # A chosen alias IS the short code (one shared space for all codes)
        CheckConstraint(
            "custom_alias IS NULL OR custom_alias = short_code",
            name="custom_alias_matches_short_code",
        ),
        Index("ix_urls_created_at", "created_at"),  # fast "newest first" lists
        # The URL list: "this owner's URLs, newest first, after this bookmark".
        # Leftmost prefix: also serves "WHERE owner_id = ?" (FK lookups, cascades),
        # so owner_id needs no index of its own.
        Index("ix_urls_owner_id_created_at_id", "owner_id", "created_at", "id"),
    )

    original_url: Mapped[str] = mapped_column(Text)
    short_code: Mapped[str] = mapped_column(String(SHORT_CODE_MAX_LENGTH), unique=True, index=True)
    custom_alias: Mapped[str | None] = mapped_column(String(SHORT_CODE_MAX_LENGTH), unique=True)
    # Indexed by ix_urls_owner_id_created_at_id (Postgres does not index FKs itself)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Soft delete: the row stays (its short code is never reused, its clicks are kept)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Filled by the URL preview service in Module 9
    title: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    favicon: Mapped[str | None] = mapped_column(Text)

    click_count: Mapped[int] = mapped_column(default=0, server_default="0")

    # lazy="raise" on every relationship: touching one that was not loaded on purpose
    # raises a clear error instead of silently sending one query per row (N+1)
    owner: Mapped["User"] = relationship(back_populates="urls", lazy="raise")
    clicks: Mapped[list["Click"]] = relationship(
        back_populates="url",
        lazy="raise",
        cascade="all, delete-orphan",
        passive_deletes=True,  # let Postgres' ON DELETE CASCADE remove the clicks
    )
    # Many-to-many through the url_tags table. No delete-orphan: deleting a URL
    # must never delete the tags other URLs still use.
    tags: Mapped[list["Tag"]] = relationship(
        secondary=url_tags,
        back_populates="urls",
        lazy="raise",
        passive_deletes=True,
    )

    # ---------- the "is this link usable?" rules, written once ----------
    # A hybrid method works in two places:
    #   url.is_live(now)  -> True/False, computed in Python on a loaded object
    #   URL.is_live(now)  -> a SQL condition, usable in select(URL).where(...)

    @hybrid_method
    def is_expired(self, now: datetime) -> bool:
        """Return True if the expiry time is reached (Python side).

        Exactly at expires_at counts as expired.
        """
        return self.expires_at is not None and self.expires_at <= now

    # ".inplace.expression" + @classmethod: SQLAlchemy 2.0's typed way to give a
    # hybrid its SQL side under a separate (private) name
    @is_expired.inplace.expression  # type: ignore[attr-defined]  # mypy sees a plain function here
    @classmethod
    def _is_expired_expression(cls, now: datetime) -> ColumnElement[bool]:
        """Build the SQL condition "expiry time is reached" (SQL side)."""
        return cls.expires_at <= now  # NULL <= now is not true, so "no expiry" never matches

    @hybrid_method
    def is_live(self, now: datetime) -> bool:
        """Return True if the link may redirect (Python side).

        Live means: not deleted, switched on and not expired.
        """
        return self.deleted_at is None and self.is_active and not self.is_expired(now)

    @is_live.inplace.expression  # type: ignore[attr-defined]  # same descriptor limitation
    @classmethod
    def _is_live_expression(cls, now: datetime) -> ColumnElement[bool]:
        """Build the SQL condition "the link may redirect" (SQL side)."""
        return and_(
            cls.deleted_at.is_(None),
            cls.is_active.is_(True),
            or_(cls.expires_at.is_(None), cls.expires_at > now),
        )

    def __repr__(self) -> str:
        """Return a short text form of the URL for logs and debugging."""
        return f"<URL id={self.id} short_code={self.short_code!r} owner_id={self.owner_id}>"


class URLManager:
    """Ready-made URL queries, like a Django "Manager".

    Use it as URL.objects.active_urls(), .expired_urls() and .popular_urls().
    Each method returns a select() statement (nothing is sent yet), so callers can
    keep adding .where(), .order_by() or .limit() and run it once, e.g.
        URL.objects.active_urls().where(URL.owner_id == 5)
    """

    @staticmethod
    def active_urls(now: datetime | None = None) -> Select[tuple[URL]]:
        """Select the live URLs.

        Live means: not deleted, switched on, and no expiry or expiry still in the future.
        """
        return select(URL).where(URL.is_live(now or datetime.now(UTC)))

    @staticmethod
    def expired_urls(now: datetime | None = None) -> Select[tuple[URL]]:
        """Select the URLs whose expiry time is reached.

        "<=" (not "<"): a URL expiring exactly now is expired,
        so no URL falls between active_urls() and expired_urls().
        """
        return select(URL).where(URL.is_expired(now or datetime.now(UTC)))

    @staticmethod
    def popular_urls(limit: int = 10) -> Select[tuple[URL]]:
        """Select the most clicked URLs first.

        The id breaks ties so the order is always the same.

        Raises:
            ValueError: If limit is not between 1 and MAX_POPULAR_LIMIT.
        """
        if not 1 <= limit <= MAX_POPULAR_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_POPULAR_LIMIT}")
        return select(URL).order_by(URL.click_count.desc(), URL.id).limit(limit)


URL.objects = URLManager()
