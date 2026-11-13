"""Helpers to read PostgreSQL error details from SQLAlchemy errors."""

from sqlalchemy.exc import IntegrityError

UNIQUE_VIOLATION = "23505"  # PostgreSQL error code (SQLSTATE) for a duplicate unique value


def unique_violation_constraint(exc: IntegrityError) -> str | None:
    """Return the name of the violated unique constraint.

    Returns:
        The constraint name (e.g. "uq_users_email") for a duplicate unique value,
        "unknown" if the name is missing, or None when the error is something
        else (FK, CHECK, ...).
    """
    if getattr(exc.orig, "sqlstate", None) != UNIQUE_VIOLATION:
        return None
    cause = getattr(exc.orig, "__cause__", None)  # the original asyncpg error
    return getattr(cause, "constraint_name", None) or "unknown"
