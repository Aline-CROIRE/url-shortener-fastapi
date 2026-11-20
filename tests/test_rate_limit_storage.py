"""Rate-limit counters live in Redis, shared by every worker (with a safe fallback).

In memory, each Gunicorn worker counts alone: with 4 workers "5 per minute" is
really 20, and a restart forgets everything. In Redis, all workers share one count.

    RATE_LIMIT_STORAGE_URI  set   -> used as it is ("redis://..." or "memory://")
                            empty -> REDIS_URL if set (Docker), else memory://

If Redis goes down, nothing breaks: the limits keep counting in memory (per worker)
and switch back once Redis answers again. Never a 500, never a hang.

The Redis tests use database 15 and only their own keys; they are skipped if Redis
is not reachable.
"""

import os
import secrets
import time
from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from limits import parse
from pydantic import ValidationError
from redis import Redis
from slowapi.errors import RateLimitExceeded

import app.core.account_limit as account_limit_module
from app.core.config import Settings
from app.core.rate_limit import build_limiter, rate_limit_exceeded_handler

REDIS_TEST_URL = os.getenv("REDIS_TEST_URL", "redis://127.0.0.1:6379/15")
DEAD_REDIS_URL = "redis://127.0.0.1:1/0"  # nothing listens on port 1


def redis_is_reachable() -> bool:
    try:
        client = Redis.from_url(REDIS_TEST_URL, socket_connect_timeout=0.5)
        client.ping()
        client.close()
        return True
    except Exception:
        return False


needs_redis = pytest.mark.skipif(
    not redis_is_reachable(), reason=f"Redis not reachable at {REDIS_TEST_URL}"
)


# ---------- which storage is used ----------


def settings(**values: str) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type,call-arg]


def test_with_no_redis_the_counters_stay_in_memory() -> None:
    # rate_limit_storage_uri="" explicitly: the suite itself sets it to memory://
    assert settings(redis_url="", rate_limit_storage_uri="").rate_limit_storage == "memory://"


def test_with_redis_configured_the_counters_go_to_redis() -> None:
    chosen = settings(redis_url="redis://cache:6379/0", rate_limit_storage_uri="")

    assert chosen.rate_limit_storage == "redis://cache:6379/0"


def test_an_explicit_storage_wins() -> None:
    chosen = settings(redis_url="redis://cache:6379/0", rate_limit_storage_uri="memory://")

    assert chosen.rate_limit_storage == "memory://"


@pytest.mark.parametrize("uri", ["http://cache:6379", "mongodb://x", "redis-cache"])
def test_an_unknown_storage_stops_the_app_at_startup(uri: str) -> None:
    with pytest.raises(ValidationError, match="RATE_LIMIT_STORAGE_URI"):
        settings(rate_limit_storage_uri=uri)


def test_the_test_suite_counts_in_memory() -> None:
    # The suite must never touch (or reset) the developer's real Redis counters
    from app.core.rate_limit import limiter

    assert type(limiter.limiter.storage).__name__ == "MemoryStorage"


# ---------- Redis: every worker shares one count ----------


@pytest.fixture
def redis_prefix() -> Iterator[str]:
    """A unique name for one test's counters; its keys are deleted afterwards.

    It goes into the counter's own identifiers: slowapi's key_prefix only applies
    to keys slowapi builds itself, not to direct counter calls like these.
    """
    prefix = f"test-limits-{secrets.token_hex(4)}"
    yield prefix
    client = Redis.from_url(REDIS_TEST_URL)
    for key in client.scan_iter(f"*{prefix}*"):
        client.delete(key)
    client.close()


@needs_redis
def test_two_workers_share_one_count_in_redis(redis_prefix: str) -> None:
    worker_one = build_limiter(REDIS_TEST_URL, key_prefix=redis_prefix)
    worker_two = build_limiter(REDIS_TEST_URL, key_prefix=redis_prefix)
    five_a_minute = parse("5/minute")

    for _ in range(3):
        assert worker_one.limiter.hit(five_a_minute, redis_prefix, "1.2.3.4")
    for _ in range(2):
        assert worker_two.limiter.hit(five_a_minute, redis_prefix, "1.2.3.4")

    # 3 + 2 = 5 already used: in memory each worker would still allow 2 or 3 more
    assert not worker_one.limiter.hit(five_a_minute, redis_prefix, "1.2.3.4")
    assert not worker_two.limiter.hit(five_a_minute, redis_prefix, "1.2.3.4")


@needs_redis
def test_counters_survive_a_restart(redis_prefix: str) -> None:
    before = build_limiter(REDIS_TEST_URL, key_prefix=redis_prefix)
    one_a_minute = parse("1/minute")
    assert before.limiter.hit(one_a_minute, redis_prefix, "1.2.3.4")

    after_restart = build_limiter(REDIS_TEST_URL, key_prefix=redis_prefix)  # a new process

    assert not after_restart.limiter.hit(one_a_minute, redis_prefix, "1.2.3.4")


# ---------- Redis down: still limited, never a 500 or a hang ----------


def app_with(limiter_storage: str) -> FastAPI:
    """A tiny app with one endpoint limited to 2/minute, on the given storage."""
    limiter = build_limiter(limiter_storage, key_prefix="fallback-test")
    test_app = FastAPI()
    test_app.state.limiter = limiter
    test_app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

    @test_app.get("/limited")
    @limiter.limit("2/minute")
    async def limited(request: Request) -> dict[str, bool]:
        return {"ok": True}

    return test_app


def test_with_redis_down_the_limit_still_works_in_memory() -> None:
    client = TestClient(app_with(DEAD_REDIS_URL), raise_server_exceptions=False)
    start = time.monotonic()

    codes = [client.get("/limited").status_code for _ in range(3)]

    assert codes == [200, 200, 429]  # counted in memory: still limited, no 500
    assert time.monotonic() - start < 5  # short timeouts: a dead Redis never hangs a request


def test_the_account_limit_also_survives_redis_going_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dead = build_limiter(DEAD_REDIS_URL, key_prefix="fallback-test")
    monkeypatch.setattr(account_limit_module, "limiter", dead)
    email = f"{secrets.token_hex(4)}@example.com"

    for _ in range(10):
        account_limit_module.record_failure(email)  # no exception: counted in memory

    assert account_limit_module.is_blocked(email) is True
    account_limit_module.clear_failures(email)
    assert account_limit_module.is_blocked(email) is False
