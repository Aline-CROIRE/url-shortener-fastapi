"""The Celery click task: saves one click and adds 1 to click_count, in ONE transaction.

The task runs on a synchronous psycopg connection (like the real worker) that joins
a test transaction rolled back at the end (see the worker_db fixture).
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import URL, Click, User
from app.services import clicks as clicks_module
from app.services.clicks import ClickEvent
from app.worker import db as worker_db_module
from app.worker.tasks import MAX_RETRIES, track_click_task

_numbers = count(1)
CLICK_TIME = datetime(2026, 11, 27, 9, 30, tzinfo=UTC)


def seed_url(session: Session) -> URL:
    n = next(_numbers)
    owner = User(email=f"task{n}@example.com", username=f"task{n}", hashed_password="!")
    url = URL(original_url="https://www.python.org/", short_code=f"task{n}", owner=owner)
    session.add(url)
    session.commit()  # only releases a savepoint: the test still rolls everything back
    return url


def saved(session: Session, url: URL) -> tuple[int, list[Click]]:
    """(click_count, clicks) as the database has them now."""
    session.expire_all()  # forget cached values: read what the task wrote
    click_count = session.scalar(select(URL.click_count).where(URL.id == url.id))
    rows = session.scalars(select(Click).where(Click.url_id == url.id)).all()
    return click_count or 0, list(rows)


def run_task(*args: Any) -> Any:
    """Run the task in THIS thread, like a worker would (retries included)."""
    return track_click_task.apply(args=args).get()


# ---------- the happy path ----------


def test_task_saves_the_click_and_adds_one(worker_db: Session) -> None:
    url = seed_url(worker_db)
    updated_before = url.updated_at

    result = run_task(
        url.id, "203.0.113.9", "Mozilla/5.0", "https://x.com/", CLICK_TIME.isoformat()
    )

    assert result is True
    click_count, rows = saved(worker_db, url)
    assert click_count == 1
    [click] = rows
    assert str(click.ip_address) == "203.0.113.9"
    assert click.user_agent == "Mozilla/5.0"
    assert click.referrer == "https://x.com/"
    # The time of the VISIT, not of the moment the worker got to it
    assert click.clicked_at == CLICK_TIME
    worker_db.refresh(url)
    assert url.updated_at == updated_before  # a visit is not an edit


def test_every_task_counts(worker_db: Session) -> None:
    url = seed_url(worker_db)

    for _ in range(3):
        run_task(url.id, None, None, None, None)

    click_count, rows = saved(worker_db, url)
    assert click_count == 3
    assert len(rows) == 3


def test_without_a_time_the_database_clock_is_used(worker_db: Session) -> None:
    url = seed_url(worker_db)

    run_task(url.id, None, None, None, None)

    [click] = saved(worker_db, url)[1]
    assert abs(click.clicked_at - datetime.now(UTC)) < timedelta(minutes=1)


# ---------- never trust a message from the queue ----------


@pytest.mark.parametrize("bad_ip", ["not-an-ip", "999.1.1.1", ""])
def test_bad_ip_from_the_queue_is_stored_as_unknown(worker_db: Session, bad_ip: str) -> None:
    url = seed_url(worker_db)

    run_task(url.id, bad_ip, None, None, None)

    [click] = saved(worker_db, url)[1]
    assert click.ip_address is None


@pytest.mark.parametrize("bad_time", ["yesterday", "2026-13-01T00:00:00"])
def test_bad_time_from_the_queue_uses_the_database_clock(worker_db: Session, bad_time: str) -> None:
    url = seed_url(worker_db)

    run_task(url.id, None, None, None, bad_time)

    [click] = saved(worker_db, url)[1]
    assert abs(click.clicked_at - datetime.now(UTC)) < timedelta(minutes=1)


def test_time_without_a_zone_is_read_as_utc() -> None:
    event = ClickEvent.from_task_args(1, None, None, None, "2026-11-27T09:30:00")

    assert event.clicked_at == CLICK_TIME


def test_task_arguments_are_plain_json_values() -> None:
    event = ClickEvent(1, "203.0.113.9", "Mozilla/5.0", "https://x.com/", CLICK_TIME)

    args = event.task_args()

    # Exactly what the JSON serializer sends to Redis: ids and strings, no objects
    assert json.loads(json.dumps(args)) == list(args)
    assert ClickEvent.from_task_args(*args) == event


# ---------- errors ----------


def test_click_for_a_url_that_no_longer_exists_is_dropped(
    worker_db: Session, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        result = run_task(999_999, None, None, None, None)

    assert result is False  # dropped: retrying could never succeed
    assert "url_id=999999" in caplog.text
    assert worker_db.scalar(select(Click).where(Click.url_id == 999_999)) is None


def test_database_hiccup_is_retried_and_counted_once(
    worker_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = seed_url(worker_db)
    real_statements = clicks_module.click_statements
    failures = iter([True, True])  # the first two tries fail, the third works

    def flaky_statements(event: ClickEvent) -> Any:
        insert, update = real_statements(event)
        if next(failures, False):
            # Fails AFTER the INSERT ran: the whole try must be rolled back
            return insert, _raise_operational_error()
        return insert, update

    monkeypatch.setattr("app.worker.tasks.click_statements", flaky_statements)

    run_task(url.id, None, None, None, None)

    click_count, rows = saved(worker_db, url)
    assert click_count == 1
    assert len(rows) == 1  # the failed tries left no half-saved click behind


def test_task_gives_up_after_max_retries(
    worker_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = seed_url(worker_db)
    tries = []

    def always_failing(event: ClickEvent) -> Any:
        tries.append(1)
        raise OperationalError("UPDATE urls ...", {}, Exception("server closed the connection"))

    monkeypatch.setattr("app.worker.tasks.click_statements", always_failing)

    with pytest.raises(OperationalError):
        run_task(url.id, None, None, None, None)

    assert len(tries) == 1 + MAX_RETRIES
    assert saved(worker_db, url) == (0, [])


def test_other_errors_are_not_retried(worker_db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    url = seed_url(worker_db)
    tries = []

    def bug(event: ClickEvent) -> Any:
        tries.append(1)
        raise ValueError("a bug, not a hiccup")

    monkeypatch.setattr("app.worker.tasks.click_statements", bug)

    with pytest.raises(ValueError):
        run_task(url.id, None, None, None, None)

    assert len(tries) == 1  # retrying a bug only repeats it


def test_retry_settings() -> None:
    assert OperationalError in track_click_task.autoretry_for
    assert track_click_task.max_retries == MAX_RETRIES == 5
    assert track_click_task.retry_backoff is True  # waits 1 s, 2 s, 4 s, ...
    assert track_click_task.retry_jitter is True  # random spread: retries do not all hit at once


def test_delay_in_eager_mode_runs_the_task(worker_db: Session, celery_eager: None) -> None:
    url = seed_url(worker_db)

    # The exact call the API makes (see app/worker/tracker.py)
    track_click_task.delay(*ClickEvent(url.id, clicked_at=CLICK_TIME).task_args())

    assert saved(worker_db, url)[0] == 1


def _raise_operational_error() -> Any:
    """A statement that fails like a deadlock (SQLSTATE 40P01) when the task runs it."""
    return text("DO $$ BEGIN RAISE EXCEPTION 'simulated deadlock' USING ERRCODE = '40P01'; END $$")


def test_other_integrity_errors_are_not_hidden(
    worker_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = seed_url(worker_db)

    def breaks_a_check(event: ClickEvent) -> Any:
        # click_count >= 0 is a CHECK constraint: a bug, not a missing URL
        return (text(f"UPDATE urls SET click_count = -1 WHERE id = {event.url_id}"),)

    monkeypatch.setattr("app.worker.tasks.click_statements", breaks_a_check)

    with pytest.raises(IntegrityError):
        run_task(url.id, None, None, None, None)


def test_worker_engine_uses_psycopg_and_a_small_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/urls")
    get_settings.cache_clear()
    worker_db_module.get_session_factory.cache_clear()
    try:
        engine = worker_db_module.get_session_factory().kw["bind"]  # no connection opened yet

        assert engine.url.drivername == "postgresql+psycopg"
        assert engine.url.database == "urls"
        assert engine.pool.size() == worker_db_module.WORKER_POOL_SIZE
        assert worker_db_module.get_session_factory().kw["bind"] is engine  # once per process
        engine.dispose()
    finally:
        worker_db_module.get_session_factory.cache_clear()
        get_settings.cache_clear()


def test_the_briefs_exact_call_works(worker_db: Session, celery_eager: None) -> None:
    url = seed_url(worker_db)

    # Word for word from the Module 8 brief: track_click_task.delay(url_id, ip_address, user_agent)
    track_click_task.delay(url.id, "203.0.113.9", "Mozilla/5.0")

    click_count, [click] = saved(worker_db, url)
    assert click_count == 1
    assert str(click.ip_address) == "203.0.113.9"
    assert click.user_agent == "Mozilla/5.0"
    assert click.referrer is None  # not sent: "Direct" in the analytics
    assert abs(click.clicked_at - datetime.now(UTC)) < timedelta(minutes=1)
