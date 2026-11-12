from datetime import datetime

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Gives every index and constraint a predictable name, e.g. "uq_users_email".
# Alembic needs names to drop or change constraints later (in downgrades).
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Parent class of every table model. Its metadata is the list of all tables."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """Adds id, created_at and updated_at to any model that inherits it."""

    # After INSERT and UPDATE, read the values Postgres filled in (created_at,
    # updated_at) back with RETURNING. Without it, touching url.updated_at after
    # an UPDATE would need a hidden reload, which async code cannot do.
    __mapper_args__ = {"eager_defaults": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    # server_default: Postgres fills the time itself when the row is inserted
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # onupdate: SQLAlchemy sets a new time whenever the row is updated through the ORM
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
