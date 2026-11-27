"""gunicorn.conf.py: worker count, timeouts, JSON logs, and the database connection math."""

import os
import runpy
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.capacity import (
    RESERVED_CONNECTIONS,
    check_connection_budget,
    connections_needed,
    default_web_workers,
)
from app.core.config import get_settings
from app.core.logging import JsonFormatter

CONF = Path(__file__).resolve().parent.parent / "gunicorn.conf.py"


@pytest.fixture
def load_conf(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Run gunicorn.conf.py with the given environment, like Gunicorn does at startup."""

    def load(**env: str) -> dict[str, Any]:
        for name in ("WEB_CONCURRENCY", "DB_POOL_SIZE", "DB_MAX_OVERFLOW", "CELERY_CONCURRENCY"):
            monkeypatch.delenv(name, raising=False)
        # setenv first, so the test's end restores "not set" even after gunicorn.conf.py sets it
        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", "placeholder")
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR")
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        get_settings.cache_clear()
        return runpy.run_path(str(CONF))

    yield load
    get_settings.cache_clear()


# ---------- workers ----------


def test_web_concurrency_sets_the_worker_count(load_conf: Any) -> None:
    assert load_conf(WEB_CONCURRENCY="3")["workers"] == 3


def test_default_worker_count_comes_from_the_cpu_cores(
    load_conf: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: 2)

    assert load_conf()["workers"] == 5  # 2 x 2 cores + 1


@pytest.mark.parametrize(("cores", "workers"), [(1, 3), (2, 5), (4, 9)])
def test_default_web_workers(cores: int, workers: int) -> None:
    assert default_web_workers(cores) == workers


def test_unknown_core_count_counts_as_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.cpu_count", lambda: None)  # the OS could not tell

    assert default_web_workers() == 3


def test_uvicorn_workers_and_timeouts(load_conf: Any) -> None:
    conf = load_conf(WEB_CONCURRENCY="2")

    assert conf["worker_class"] == "uvicorn_worker.UvicornWorker"  # ASGI inside Gunicorn
    assert conf["bind"] == "0.0.0.0:8000"
    assert conf["timeout"] == 30  # a stuck worker is restarted
    assert conf["graceful_timeout"] == 30  # time to finish open requests on shutdown
    assert conf["keepalive"] == 5
    assert conf["max_requests"] > 0  # workers are replaced now and then (slow memory leaks)
    assert conf["max_requests_jitter"] > 0  # ... not all at the same moment


def test_gunicorn_logs_are_json_and_its_access_log_is_off(load_conf: Any) -> None:
    conf = load_conf(WEB_CONCURRENCY="2")

    assert conf["accesslog"] is None  # our middleware writes the access line
    formatter = conf["logconfig_dict"]["formatters"]["json"]["()"]
    assert formatter is JsonFormatter


# ---------- the database connection math ----------


def test_connections_needed() -> None:
    # 4 web workers x (3 pooled + 2 overflow) + 2 Celery processes x 2 = 24
    assert connections_needed(4, pool_size=3, max_overflow=2, celery_processes=2) == 24


def test_too_many_workers_refuse_to_start(load_conf: Any) -> None:
    # 40 x (3 + 2) = 200 connections: PostgreSQL (100) would refuse some of them
    with pytest.raises(SystemExit, match="max_connections"):
        load_conf(WEB_CONCURRENCY="40")


def test_budget_keeps_room_for_admin_connections() -> None:
    settings = get_settings().model_copy(
        update={"db_max_connections": 100, "db_pool_size": 3, "db_max_overflow": 2}
    )
    celery = settings.celery_concurrency * 2
    fits = (100 - RESERVED_CONNECTIONS - celery) // 5

    assert check_connection_budget(settings, fits) <= 100 - RESERVED_CONNECTIONS
    with pytest.raises(ValueError, match="max_connections"):
        check_connection_budget(settings, fits + 1)


def test_pool_settings_reach_the_engine(test_settings: Any) -> None:
    from app.db.database import create_engine

    engine = create_engine(test_settings.database_url, pool_size=4, max_overflow=1, pool_timeout=7)

    assert engine.pool.size() == 4
    assert engine.pool._max_overflow == 1  # type: ignore[attr-defined]
    assert engine.pool._timeout == 7  # type: ignore[attr-defined]


# ---------- Prometheus multiprocess mode ----------


def test_workers_get_a_shared_metrics_folder(load_conf: Any) -> None:
    conf = load_conf(WEB_CONCURRENCY="2")

    # Set in the master BEFORE the workers start: prometheus_client reads it at import
    assert os.environ["PROMETHEUS_MULTIPROC_DIR"] == conf["PROMETHEUS_DIR"]


def test_a_metrics_folder_given_by_the_environment_is_kept(load_conf: Any, tmp_path: Path) -> None:
    conf = load_conf(WEB_CONCURRENCY="2", PROMETHEUS_MULTIPROC_DIR=str(tmp_path))

    assert conf["PROMETHEUS_DIR"] == str(tmp_path)


def test_startup_removes_numbers_left_from_the_last_run(load_conf: Any, tmp_path: Path) -> None:
    folder = tmp_path / "metrics"
    folder.mkdir()
    (folder / "counter_4242.db").write_bytes(b"old numbers")
    (folder / "notes.txt").write_text("not ours")
    conf = load_conf(WEB_CONCURRENCY="2", PROMETHEUS_MULTIPROC_DIR=str(folder))

    conf["on_starting"](server=None)

    assert not (folder / "counter_4242.db").exists()  # counts restart from zero
    assert (folder / "notes.txt").exists()  # only metric files are touched


def test_startup_creates_the_metrics_folder(load_conf: Any, tmp_path: Path) -> None:
    folder = tmp_path / "new" / "metrics"
    conf = load_conf(WEB_CONCURRENCY="2", PROMETHEUS_MULTIPROC_DIR=str(folder))

    conf["on_starting"](server=None)

    assert folder.is_dir()


def test_a_dead_worker_no_longer_counts_as_in_progress(load_conf: Any, tmp_path: Path) -> None:
    conf = load_conf(WEB_CONCURRENCY="2", PROMETHEUS_MULTIPROC_DIR=str(tmp_path))
    (tmp_path / "gauge_livesum_4242.db").write_bytes(b"requests running in worker 4242")
    (tmp_path / "counter_4242.db").write_bytes(b"requests counted by worker 4242")

    conf["child_exit"](server=None, worker=SimpleNamespace(pid=4242))

    assert not (tmp_path / "gauge_livesum_4242.db").exists()  # "live" values die with it
    assert (tmp_path / "counter_4242.db").exists()  # what it counted still counts


def test_gunicorn_threads_are_not_used(load_conf: Any) -> None:
    conf = load_conf(WEB_CONCURRENCY="2")

    # Gunicorn's "threads" only works with its sync/gthread workers; a UvicornWorker
    # ignores it. Blocking work uses AnyIO's pool (THREAD_POOL_SIZE) instead.
    assert "threads" not in conf


def test_workers_are_recycled_rarely(load_conf: Any) -> None:
    conf = load_conf(WEB_CONCURRENCY="2")

    # Load test: a worker reaching max_requests closes its keep-alive connections, and
    # clients that reuse one at that moment get "connection reset" (8 of 3709 requests
    # with max_requests=1000). The profile showed no memory leak, so recycling is a
    # rare safety net, not a routine.
    assert conf["max_requests"] >= 10_000
    assert conf["max_requests_jitter"] >= conf["max_requests"] // 10


def test_unused_control_socket_is_off(load_conf: Any) -> None:
    conf = load_conf(WEB_CONCURRENCY="2")

    # Gunicorn 26's runtime control socket (for gunicornc) is not used: Docker and the
    # orchestrator manage the processes. Off = no error (the app user has no home
    # folder for the socket) and one admin interface less.
    assert conf["control_socket_disable"] is True
