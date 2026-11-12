from typing import TYPE_CHECKING

from sqlalchemy import Column, ForeignKey, Index, String, Table, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:  # only for type checkers: avoids a circular import at runtime
    from app.models.url import URL

TAG_NAME_MAX_LENGTH = 50

# The "sign-up sheet": one row per (URL, tag) pair. A plain Table, not a model
# class, because a row holds no data of its own besides the two ids.
url_tags = Table(
    "url_tags",
    Base.metadata,
    # Both columns together form the primary key: the same pair cannot repeat.
    # The PK index starts with url_id, so "tags of this URL" is already fast.
    Column("url_id", ForeignKey("urls.id", ondelete="CASCADE"), primary_key=True),
    # "URLs with this tag" needs an index that starts with tag_id
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True, index=True),
)


class Tag(TimestampMixin, Base):
    """A label (e.g. "Marketing") shared by all users and attached to many URLs."""

    __tablename__ = "tags"

    name: Mapped[str] = mapped_column(String(TAG_NAME_MAX_LENGTH), unique=True)

    urls: Mapped[list["URL"]] = relationship(
        secondary=url_tags,
        back_populates="tags",
        lazy="raise",  # never loaded behind our back (see F3.1)
        # Deleting a tag removes only its url_tags rows (Postgres CASCADE),
        # never the URLs. No ORM cascade: tags and URLs live independently.
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<Tag id={self.id} name={self.name!r}>"


# Case-insensitive uniqueness: "Marketing" and "marketing" are the same tag.
# An index on an expression (lower(name)) must be declared after the class.
Index("uq_tags_name_lower", func.lower(Tag.name), unique=True)
