"""The nightly archive: switch off expired URLs in ONE bulk UPDATE, then clear their cache keys."""

import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any

import pytest
from celery.schedules import crontab
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import Engine, create_engine, delete, event, select, text, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.cache import RedisRedirectCache
from app.core.config import Settings, get_settings, to_sync_url
from app.models import URL, User
from app.worker.cache import get_redis
from app.worker.celery_app import celery_app
from app.worker.tasks import archive_expired_urls_task

API = "/api/v1/urls"
NOW = datetime.now(UTC)
PAST = NOW - timedelta(hours=1)
FUTURE = NOW + timedelta(days=1)
_numbers = count(1)


class FakeSyncRedis:
    """Just enough of redis.Redis (sync, like the worker): DELETE of many keys."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.down = False

    def delete(self, *keys: str) -> int:
        if self.down:
            raise RedisConnectionError("Connection refused")
        return sum(self.data.pop(key, None) is not None for key in keys)


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeSyncRedis:
    fake = FakeSyncRedis()
    monkeypatch.setattr("app.worker.tasks.get_redis", lambda: fake)
    return fake


def seed(session: Session, **fields: Any) -> URL:
    n = next(_numbers)
    owner = User(email=f"arch{n}@example.com", username=f"arch{n}", hashed_password="!")
    url = URL(original_url="https://www.python.org/", short_code=f"arch{n}", owner=owner, **fields)
    session.add(url)
    session.commit()
    return url


def is_active(session: Session, url: URL) -> bool:
    session.expire_all()
    return bool(session.scalar(select(URL.is_active).where(URL.id == url.id)))


def run_archive() -> int:
    return archive_expired_urls_task.apply().get()


# ---------- what is archived ----------


def test_archives_only_live_links_whose_time_is_up(
    worker_db: Session, fake_redis: FakeSyncRedis
) -> None:
    expired = seed(worker_db, expires_at=PAST)
    future = seed(worker_db, expires_at=FUTURE)
    no_expiry = seed(worker_db)
    deleted = seed(worker_db, expires_at=PAST, deleted_at=PAST)

    archived = run_archive()

    assert archived >= 1
    assert is_active(worker_db, expired) is False
    assert is_active(worker_db, future) is True
    assert is_active(worker_db, no_expiry) is True
    assert is_active(worker_db, deleted) is True  # deleted links are left as they are


def test_already_switched_off_links_are_not_counted_again(
    worker_db: Session, fake_redis: FakeSyncRedis
) -> None:
    seed(worker_db, expires_at=PAST)
    run_archive()

    assert run_archive() == 0  # safe to run twice (e.g. after a retry)


def test_link_expiring_exactly_now_is_archived(
    worker_db: Session, fake_redis: FakeSyncRedis
) -> None:
    # Same rule as the redirect (URL.is_expired): expires_at <= now is expired.
    # The database clock moves on, so "now" here is already in the past for it.
    url = seed(worker_db, expires_at=datetime.now(UTC))

    run_archive()

    assert is_active(worker_db, url) is False


def test_archive_is_one_bulk_update(worker_db: Session, fake_redis: FakeSyncRedis) -> None:
    for _ in range(5):
        seed(worker_db, expires_at=PAST)
    statements: list[str] = []
    engine = worker_db.get_bind().engine

    def remember(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", remember)
    try:
        assert run_archive() >= 5
    finally:
        event.remove(engine, "before_cursor_execute", remember)

    # ONE UPDATE for all rows (plus the savepoint commands of the test transaction),
    # never one UPDATE per URL
    updates = [s for s in statements if s.lstrip().upper().startswith("UPDATE")]
    assert len(updates) == 1, statements
    assert "RETURNING" in updates[0].upper()  # the short codes come back in the same trip


def test_logs_how_many_were_archived(
    worker_db: Session, fake_redis: FakeSyncRedis, caplog: pytest.LogCaptureFixture
) -> None:
    seed(worker_db, expires_at=PAST)

    with caplog.at_level(logging.INFO):
        archived = run_archive()

    assert f"Archived {archived} expired URLs" in caplog.text


# ---------- the cache ----------


def test_clears_the_cache_keys_of_archived_links_only(
    worker_db: Session, fake_redis: FakeSyncRedis
) -> None:
    expired = seed(worker_db, expires_at=PAST)
    live = seed(worker_db, expires_at=FUTURE)
    fake_redis.data[RedisRedirectCache.key(expired.short_code)] = "{...}"
    fake_redis.data[RedisRedirectCache.key(live.short_code)] = "{...}"

    run_archive()

    assert RedisRedirectCache.key(expired.short_code) not in fake_redis.data
    assert RedisRedirectCache.key(live.short_code) in fake_redis.data


def test_redis_down_does_not_undo_the_archive(
    worker_db: Session, fake_redis: FakeSyncRedis, caplog: pytest.LogCaptureFixture
) -> None:
    url = seed(worker_db, expires_at=PAST)
    fake_redis.down = True

    with caplog.at_level(logging.WARNING):
        run_archive()

    assert is_active(worker_db, url) is False  # the database change is kept
    assert "Could not clear" in caplog.text


def test_without_redis_the_archive_still_works(
    worker_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.worker.tasks.get_redis", lambda: None)  # REDIS_URL is empty
    url = seed(worker_db, expires_at=PAST)

    run_archive()

    assert is_active(worker_db, url) is False


# ---------- the schedule ----------


def test_beat_runs_the_archive_every_night_at_two_utc() -> None:
    entry = celery_app.conf.beat_schedule["archive-expired-urls"]

    assert entry["task"] == archive_expired_urls_task.name
    assert entry["schedule"] == crontab(hour=2, minute=0)
    assert celery_app.conf.timezone == "UTC"
    assert archive_expired_urls_task.name in celery_app.tasks


# ---------- what visitors and owners see ----------


def test_archived_link_still_says_expired(db_client: TestClient, run_db: Callable) -> None:
    async def archived_link(session: Any) -> str:
        owner = User(email="arch-http@example.com", username="arch-http", hashed_password="!")
        url = URL(
            original_url="https://www.python.org/",
            short_code="archhttp",
            owner=owner,
            expires_at=PAST,
            is_active=False,  # what the nightly job does
        )
        session.add(url)
        return "archhttp"

    code = run_db(archived_link)

    response = db_client.get(f"/{code}")

    assert response.status_code == 410
    assert response.json()["error"]["code"] == "url_expired"  # the real reason


def test_owner_can_bring_an_archived_link_back(
    db_client: TestClient, owner_id: int, run_db: Callable
) -> None:
    created = db_client.post(API, json={"original_url": "https://www.python.org/"}).json()
    code = created["short_code"]

    async def archive(session: Any) -> None:
        await session.execute(
            update(URL).where(URL.short_code == code).values(expires_at=PAST, is_active=False)
        )

    run_db(archive)

    revived = db_client.put(
        f"{API}/{code}", json={"is_active": True, "expires_at": FUTURE.isoformat()}
    )

    assert revived.status_code == 200
    assert db_client.get(f"/{code}").status_code == 302


# ---------- the race with an owner who extends the link (two real connections) ----------


@pytest.fixture
def committed_db(test_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """A REAL committed test database (the race needs two connections), cleaned afterwards."""
    engine = create_engine(to_sync_url(test_settings.test_database_url), poolclass=NullPool)
    monkeypatch.setattr("app.worker.tasks.get_session_factory", lambda: sessionmaker(engine))
    monkeypatch.setattr("app.worker.tasks.get_redis", lambda: None)
    yield engine
    with engine.begin() as connection:
        connection.execute(delete(User).where(User.email.like("race-arch%")))  # cascades
    engine.dispose()


def wait_until_blocked(engine: Engine, racer: threading.Thread) -> None:
    """Return once another connection waits for a row lock (no fixed sleep)."""
    deadline = time.monotonic() + 10
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as watcher:
        while time.monotonic() < deadline:
            assert racer.is_alive(), "the archive finished without waiting for the lock"
            waiting = watcher.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE wait_event_type = 'Lock' AND datname = current_database()"
                )
            )
            if waiting:
                return
            time.sleep(0.02)
    raise AssertionError("the archive never waited for the owner's change")


def test_owner_extending_the_link_during_the_archive_wins(committed_db: Engine) -> None:
    with Session(committed_db) as session:
        owner = User(email="race-arch@example.com", username="race-arch", hashed_password="!")
        url = URL(original_url="https://a.b/", short_code="racearch", owner=owner, expires_at=PAST)
        session.add(url)
        session.commit()
        url_id = url.id

    # 1. The owner's transaction changes the expiry and holds the row lock
    owner_conn = committed_db.connect()
    owner_tx = owner_conn.begin()
    owner_conn.execute(update(URL).where(URL.id == url_id).values(expires_at=FUTURE))

    # 2. The nightly archive starts and must wait for that row
    results: list[int] = []
    racer = threading.Thread(target=lambda: results.append(run_archive()))
    racer.start()
    wait_until_blocked(committed_db, racer)

    # 3. The owner commits; Postgres re-checks the archive's WHERE on the NEW row
    owner_tx.commit()
    owner_conn.close()
    racer.join(timeout=10)

    with Session(committed_db) as session:
        assert session.scalar(select(URL.is_active).where(URL.id == url_id)) is True


@pytest.mark.parametrize(("redis_url", "has_client"), [("", False), ("redis://cache:6379/0", True)])
def test_worker_redis_client_only_when_configured(
    monkeypatch: pytest.MonkeyPatch, redis_url: str, has_client: bool
) -> None:
    monkeypatch.setenv("REDIS_URL", redis_url)
    get_settings.cache_clear()
    get_redis.cache_clear()
    try:
        client = get_redis()  # Redis connects lazily: nothing is opened here

        assert (client is not None) is has_client
        if client is not None:
            assert client.connection_pool.connection_kwargs["db"] == 0  # the cache DB
    finally:
        get_redis.cache_clear()
        get_settings.cache_clear()
