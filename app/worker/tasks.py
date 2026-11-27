"""Celery tasks: the jobs the worker takes from the queue."""

import logging

from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError

from app.db.errors import is_foreign_key_violation
from app.services.clicks import ClickEvent, click_statements
from app.worker.celery_app import celery_app
from app.worker.db import get_session_factory

logger = logging.getLogger(__name__)

MAX_RETRIES = 5
# Problems that may be gone on the next try: the database is restarting or
# unreachable (OperationalError, InterfaceError), or a deadlock (also an
# OperationalError). A bug (any other error) would fail again: no retry.
RETRYABLE_DB_ERRORS = (OperationalError, InterfaceError)


@celery_app.task(
    name="clicks.track_click",
    autoretry_for=RETRYABLE_DB_ERRORS,
    max_retries=MAX_RETRIES,
    retry_backoff=True,  # wait 1 s, 2 s, 4 s, 8 s, 16 s between tries
    retry_backoff_max=60,
    retry_jitter=True,  # random waits, so retries after an outage do not all hit at once
)
def track_click_task(
    url_id: int,
    ip_address: str | None,
    user_agent: str | None,
    referrer: str | None,
    clicked_at: str | None = None,
) -> bool:
    """Save one click and add 1 to the URL's click_count, in ONE transaction.

    Gets only simple values (ids, strings): the message travels as JSON through
    Redis, and the worker is another process with its own database connection.

    Returns:
        True if saved, False if dropped because the URL no longer exists.
    """
    event = ClickEvent.from_task_args(url_id, ip_address, user_agent, referrer, clicked_at)
    try:
        # sessionmaker.begin(): BEGIN, then COMMIT at the end, or ROLLBACK on any
        # error, so a retry never finds a half-saved click
        with get_session_factory().begin() as session:
            for statement in click_statements(event):
                session.execute(statement)
    except IntegrityError as exc:
        if not is_foreign_key_violation(exc):
            raise
        # The URL row is gone (removed outside the API): retrying cannot help
        logger.warning("Click for url_id=%s dropped: the URL no longer exists", event.url_id)
        return False
    return True
