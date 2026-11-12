from sqlalchemy.exc import IntegrityError

UNIQUE_VIOLATION = "23505"  # PostgreSQL error code (SQLSTATE) for a duplicate unique value


def unique_violation_constraint(exc: IntegrityError) -> str | None:
    """The violated constraint's name (e.g. "uq_users_email") for a duplicate
    unique value, or None when the error is something else (FK, CHECK, ...).
    """
    if getattr(exc.orig, "sqlstate", None) != UNIQUE_VIOLATION:
        return None
    return getattr(exc.orig.__cause__, "constraint_name", None) or "unknown"
