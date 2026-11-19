"""Helpers to read PostgreSQL error details from SQLAlchemy errors."""

from sqlalchemy.exc import DBAPIError, IntegrityError

UNIQUE_VIOLATION = "23505"  # PostgreSQL error code (SQLSTATE) for a duplicate unique value
# Postgres cancelled the transaction only because of other transactions running at the
# same moment; nothing was saved, so the same request can safely be sent again
RETRYABLE_SQLSTATES = frozenset({"40P01", "40001"})  # deadlock, serialization failure


def is_retryable(exc: DBAPIError) -> bool:
    """Return True if the transaction was cancelled by a deadlock or serialization failure."""
    return getattr(exc.orig, "sqlstate", None) in RETRYABLE_SQLSTATES


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
