"""Profiling script (latency percentiles, CPU, memory) and the thread pool setting."""

import asyncio
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, delete, false, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.config import Settings, to_sync_url
from app.main import create_app
from app.models import URL, User
from app.scripts import profile_redirects as profiler

# ---------- the numbers ----------


def test_latency_summary_gives_percentiles_in_milliseconds() -> None:
    samples = [ms / 1000 for ms in range(1, 101)]  # 1 ms ... 100 ms

    summary = profiler.latency_summary(samples)

    assert summary["count"] == 100
    assert summary["mean_ms"] == pytest.approx(50.5)
    assert summary["p50_ms"] == pytest.approx(50.5)
    # p95: 95% of the requests were at least this fast (the "slow tail" starts here)
    assert summary["p95_ms"] == pytest.approx(95.05)
    assert summary["p99_ms"] == pytest.approx(99.01)
    assert summary["max_ms"] == pytest.approx(100.0)


def test_latency_summary_of_one_sample() -> None:
    summary = profiler.latency_summary([0.004])

    assert summary["p50_ms"] == summary["p99_ms"] == pytest.approx(4.0)


@pytest.mark.anyio
async def test_measure_times_only_the_request() -> None:
    tiny = FastAPI()

    @tiny.get("/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "yes"}

    calls: list[int] = []

    async def slow_preparation() -> None:
        calls.append(1)
        await asyncio.sleep(0.05)  # e.g. clearing the cache: not part of the timing

    transport = httpx.ASGITransport(app=tiny)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        samples = await profiler.measure(
            client, "/ping", 5, before_each=slow_preparation, expected_status=200
        )

    assert len(samples) == 5
    assert len(calls) == 5
    assert all(0 < sample < 0.05 for sample in samples)


@pytest.mark.anyio
async def test_measure_refuses_a_failing_request() -> None:
    tiny = FastAPI()
    transport = httpx.ASGITransport(app=tiny)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        # Timing 404s would measure the wrong thing: stop with a clear message
        with pytest.raises(RuntimeError, match="404"):
            await profiler.measure(client, "/missing", 3, expected_status=302)


# ---------- the whole run, against the test database ----------


@pytest.fixture
def live_code(test_settings: Settings) -> Iterator[str]:
    """A COMMITTED live URL (the profiler opens its own connections), removed afterwards."""
    engine: Engine = create_engine(to_sync_url(test_settings.test_database_url), poolclass=NullPool)
    with Session(engine) as session:
        owner = User(email="profile-run@example.com", username="profile-run", hashed_password="!")
        session.add(URL(original_url="https://www.python.org/", short_code="profrun", owner=owner))
        session.commit()
    yield "profrun"
    with engine.begin() as connection:
        connection.execute(delete(User).where(User.email == "profile-run@example.com"))
    engine.dispose()


def test_profile_report_has_latency_cpu_and_memory(test_settings: Settings, live_code: str) -> None:
    report = asyncio.run(profiler.profile_redirects(test_settings, live_code, requests=20))

    assert "| cache hit |" in report
    assert "| cache miss |" in report
    assert "p95" in report
    assert "## CPU" in report  # cProfile: where the CPU time goes
    assert "## Memory" in report  # tracemalloc: what was allocated, growth per request
    assert "per request" in report


def test_profiling_never_sends_clicks(
    test_settings: Settings, live_code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[Any] = []
    monkeypatch.setattr("app.worker.tasks.track_click_task.delay", lambda *a: sent.append(a))

    asyncio.run(profiler.profile_redirects(test_settings, live_code, requests=5))

    assert sent == []  # thousands of fake clicks would pollute the analytics


def test_requests_must_be_positive() -> None:
    with pytest.raises(SystemExit):
        profiler.parse_args(["--requests", "0"])


# ---------- thread pool ----------


def test_thread_pool_defaults_to_anyios_40(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("THREAD_POOL_SIZE", raising=False)

    assert Settings(_env_file=None).thread_pool_size == 40  # type: ignore[call-arg]


def test_thread_pool_must_have_a_thread() -> None:
    with pytest.raises(ValidationError, match="thread_pool_size"):
        Settings(thread_pool_size=0)


def test_the_app_sets_its_thread_pool_size(test_settings: Settings) -> None:
    app = create_app(test_settings.model_copy(update={"thread_pool_size": 7}))

    with TestClient(app) as client:
        # Read inside the app's event loop: the limit belongs to that loop
        tokens = client.portal.call(
            lambda: anyio.to_thread.current_default_thread_limiter().total_tokens
        )

    assert tokens == 7


def test_slow_blocking_work_queues_when_the_pool_is_full() -> None:
    async def scenario() -> float:
        anyio.to_thread.current_default_thread_limiter().total_tokens = 2
        start = time.perf_counter()
        async with anyio.create_task_group() as group:
            for _ in range(4):  # 4 blocking jobs, 2 threads: two rounds
                group.start_soon(anyio.to_thread.run_sync, time.sleep, 0.2)
        return time.perf_counter() - start

    elapsed = anyio.run(scenario)

    # With a full pool, extra work WAITS: why a hanging broker could stall
    # every .delay() if the pool were tiny (and why publishing fails fast)
    assert elapsed >= 0.38


def test_profiling_restores_the_log_levels(
    test_settings: Settings, live_code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("app.requests", "httpx"):  # known starting levels (restored afterwards)
        monkeypatch.setattr(logging.getLogger(name), "level", logging.INFO)

    asyncio.run(profiler.profile_redirects(test_settings, live_code, requests=3))

    # Quieted only while profiling: other code (and tests) must still see these logs
    assert logging.getLogger("app.requests").level == logging.INFO
    assert logging.getLogger("httpx").level == logging.INFO


def test_refuses_to_profile_when_redis_does_not_answer(
    test_settings: Settings, live_code: str
) -> None:
    dead = test_settings.model_copy(update={"redis_url": "redis://127.0.0.1:1/0"})

    # Every lookup would wait for the timeout: "hit" and "miss" would both be fake
    with pytest.raises(SystemExit, match="Redis"):
        asyncio.run(profiler.profile_redirects(dead, live_code, requests=3))


def test_newest_live_code_is_found(test_settings: Settings, live_code: str) -> None:
    assert asyncio.run(profiler.newest_live_code(test_settings)) == live_code


def test_main_prints_and_saves_the_report(
    test_settings: Settings,
    live_code: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(profiler, "get_settings", lambda: test_settings)
    report_file = tmp_path / "profile.md"

    profiler.main(["--requests", "3", "--report", str(report_file)])  # newest live code

    assert "# Redirect profile: /profrun" in capsys.readouterr().out
    assert report_file.read_text(encoding="utf-8").startswith("# Redirect profile")


def test_without_any_live_url_the_script_explains_what_to_do(
    test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The query finds nothing (as on an empty database)
    monkeypatch.setattr(profiler, "select", lambda *columns: select(*columns).where(false()))

    with pytest.raises(SystemExit, match="No live short URL"):
        asyncio.run(profiler.newest_live_code(test_settings))


def test_main_without_report_only_prints(
    test_settings: Settings,
    live_code: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(profiler, "get_settings", lambda: test_settings)
    monkeypatch.chdir(tmp_path)

    profiler.main(["--code", live_code, "--requests", "2"])

    assert "## CPU" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []  # no file written


def test_refuses_to_time_hits_when_the_cache_did_not_fill(
    test_settings: Settings, live_code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.cache import RedisRedirectCache

    async def answers(self: Any) -> bool:
        return True

    async def never_cached(self: Any, short_code: str) -> None:
        return None  # e.g. a tombstone from a run 10 s ago blocks refilling for 30 s

    monkeypatch.setattr(RedisRedirectCache, "ping", answers)
    monkeypatch.setattr(RedisRedirectCache, "get", never_cached)
    configured = test_settings.model_copy(update={"redis_url": "redis://127.0.0.1:1/0"})

    # Otherwise the "cache hit" row would silently be misses
    with pytest.raises(SystemExit, match="not filled"):
        asyncio.run(profiler.profile_redirects(configured, live_code, requests=3))
