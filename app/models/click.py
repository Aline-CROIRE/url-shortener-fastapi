"""The Click model: one row per visit of a short link."""

from datetime import datetime
from ipaddress import IPv4Address, IPv6Address
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:  # only for type checkers: avoids a circular import at runtime
    from app.models.url import URL


class Click(Base):
    """The clicks table: one visit of a short link.

    Rows are only inserted, never updated, so there is no TimestampMixin
    (no updated_at); clicked_at is the timestamp.
    """

    __tablename__ = "clicks"
    __table_args__ = (
        # "All clicks of this URL (in a time range)": the main analytics question.
        # Leftmost-prefix rule: also serves "WHERE url_id = ?" alone, so url_id
        # needs no index of its own.
        Index("ix_clicks_url_id_clicked_at", "url_id", "clicked_at"),
    )

    # BIGINT: a busy shortener can pass 2.1 billion clicks (the INTEGER limit)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    url_id: Mapped[int] = mapped_column(ForeignKey("urls.id", ondelete="CASCADE"))
    # The database clock stamps the time, not the app
    clicked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # INET validates the address and stores IPv4/IPv6 compactly.
    # Nullable: the client address can be missing or not a real IP.
    ip_address: Mapped[IPv4Address | IPv6Address | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    referrer: Mapped[str | None] = mapped_column(Text)
    # Filled later by a GeoIP lookup; country is the ISO 3166-1 alpha-2 code, e.g. "RW"
    country: Mapped[str | None] = mapped_column(String(2))
    city: Mapped[str | None] = mapped_column(String(100))

    # lazy="raise": never loaded behind our back; ask with joinedload(Click.url)
    url: Mapped["URL"] = relationship(back_populates="clicks", lazy="raise")

    def __repr__(self) -> str:
        """Return a short text form of the click for logs and debugging."""
        return f"<Click id={self.id} url_id={self.url_id} clicked_at={self.clicked_at}>"
