"""Celery tasks: the jobs the worker takes from the queue."""

import logging
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError

from app.db.errors import is_foreign_key_violation
from app.repositories.url import URLRepository
from app.services.clicks import ClickEvent, click_statements
from app.worker.cache import clear_cached_redirects, get_redis
from app.worker.celery_app import celery_app
from app.worker.db import get_session_factory
from app.worker.geoip import get_geoip_reader, locate

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
    referrer: str | None = None,
    clicked_at: str | None = None,
) -> bool:
    """Save one click and add 1 to the URL's click_count, in ONE transaction.

    Gets only simple values (ids, strings): the message travels as JSON through
    Redis, and the worker is another process with its own database connection.
    The brief's call track_click_task.delay(url_id, ip_address, user_agent) works
    as written; the API also sends the referrer and the visit time.

    Returns:
        True if saved, False if dropped because the URL no longer exists.
    """
    event = ClickEvent.from_task_args(url_id, ip_address, user_agent, referrer, clicked_at)
    # Country and city from the IP (an in-memory lookup, no network); UNKNOWN without a database
    location = locate(get_geoip_reader(), event.ip_address)
    try:
        # sessionmaker.begin(): BEGIN, then COMMIT at the end, or ROLLBACK on any
        # error, so a retry never finds a half-saved click
        with get_session_factory().begin() as session:
            for statement in click_statements(event, location.country, location.city):
                session.execute(statement)
    except IntegrityError as exc:
        if not is_foreign_key_violation(exc):
            raise
        # The URL row is gone (removed outside the API): retrying cannot help
        logger.warning("Click for url_id=%s dropped: the URL no longer exists", event.url_id)
        return False
    return True


@celery_app.task(
    name="urls.archive_expired",
    autoretry_for=RETRYABLE_DB_ERRORS,
    max_retries=3,
    retry_backoff=True,
    retry_jitter=True,
)
def archive_expired_urls_task() -> int:
    """Switch off (archive) every expired URL; run every night by Celery beat.

    Safe to run twice: a second run finds nothing left to archive.

    Returns:
        How many URLs were archived.
    """
    now = datetime.now(UTC)
    # ONE bulk UPDATE ... RETURNING short_code, committed at the end of the block
    with get_session_factory().begin() as session:
        short_codes = list(session.scalars(URLRepository.archive_expired_statement(now)))
    # AFTER the commit: their cached redirects must go (they are no longer live)
    clear_cached_redirects(get_redis(), short_codes)
    logger.info("Archived %d expired URLs", len(short_codes))
    return len(short_codes)
