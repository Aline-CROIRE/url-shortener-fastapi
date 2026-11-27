"""Worker metrics: what the Celery worker did (tasks, clicks, archive), served on its own port."""

import logging
import os
import socket
import subprocess
import sys
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.worker import metrics as worker_metrics
from app.worker.tasks import archive_expired_urls_task, track_click_task
from tests.test_archive import PAST, FakeSyncRedis, seed
from tests.test_click_task import seed_url

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLICK_TASK = "clicks.track_click"


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def tasks_total(outcome: str, task: str = CLICK_TASK) -> float:
    return sample("celery_tasks_total", task=task, outcome=outcome)


# ---------- what is counted ----------


def test_a_task_run_is_counted_and_timed(worker_db: Session) -> None:
    url = seed_url(worker_db)
    succeeded = tasks_total("success")
    timed = sample("celery_task_duration_seconds_count", task=CLICK_TASK)
    saved = sample("clicks_saved_total", outcome="saved")

    track_click_task.apply(args=(url.id, None, None)).get()

    assert tasks_total("success") == succeeded + 1
    assert sample("celery_task_duration_seconds_count", task=CLICK_TASK) == timed + 1
    assert sample("clicks_saved_total", outcome="saved") == saved + 1


def test_a_dropped_click_is_counted(worker_db: Session) -> None:
    dropped = sample("clicks_saved_total", outcome="dropped")

    track_click_task.apply(args=(999_999, None, None)).get()  # the URL does not exist

    assert sample("clicks_saved_total", outcome="dropped") == dropped + 1


def test_retries_and_failures_are_counted(
    worker_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = seed_url(worker_db)
    deadlock = text("DO $$ BEGIN RAISE EXCEPTION 'x' USING ERRCODE = '40P01'; END $$")

    def always_deadlocks(event: Any, *location: Any) -> Any:
        return (deadlock,)

    monkeypatch.setattr("app.worker.tasks.click_statements", always_deadlocks)
    retried, failed = tasks_total("retry"), tasks_total("failure")

    with pytest.raises(Exception):  # noqa: B017 - the deadlock, after the last retry
        track_click_task.apply(args=(url.id, None, None)).get()

    # A growing "retry" rate = the database is struggling; "failure" = clicks lost
    assert tasks_total("retry") == retried + track_click_task.max_retries
    assert tasks_total("failure") == failed + 1


def test_archived_urls_are_counted(worker_db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.worker.tasks.get_redis", lambda: FakeSyncRedis())
    seed(worker_db, expires_at=PAST)
    seed(worker_db, expires_at=PAST)
    before = sample("urls_archived_total")

    archived = archive_expired_urls_task.apply().get()

    assert archived >= 2
    assert sample("urls_archived_total") == before + archived


# ---------- the worker's own /metrics ----------


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture
def metrics_server(tmp_path: Path) -> Iterator[tuple[Path, int]]:
    port = free_port()
    server = worker_metrics.start_metrics_server(str(tmp_path), port, address="127.0.0.1")
    assert server is not None
    yield tmp_path, port
    server.shutdown()


COUNT_ONE_TASK = """
from app.worker.metrics import TASKS
TASKS.labels(task="clicks.track_click", outcome="success").inc()
"""


def test_the_worker_serves_the_sum_of_its_processes(metrics_server: tuple[Path, int]) -> None:
    folder, port = metrics_server
    env = {
        "PROMETHEUS_MULTIPROC_DIR": str(folder),
        "JWT_SECRET_KEY": "k" * 32,
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
    }
    for _ in range(3):  # three pool processes, one task each
        subprocess.run(
            [sys.executable, "-c", COUNT_ONE_TASK], cwd=PROJECT_ROOT, env=env, check=True
        )

    body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read().decode()

    assert 'celery_tasks_total{outcome="success",task="clicks.track_click"} 3.0' in body


IMPORT_THE_TASKS = "import app.worker.tasks"


def test_the_metrics_folder_must_exist_before_python_starts(tmp_path: Path) -> None:
    env = {"JWT_SECRET_KEY": "k" * 32, "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}

    # Seen live: prometheus_client creates its files when a metric is DEFINED (at
    # import), so a missing folder crashes the worker before it starts
    missing = subprocess.run(
        [sys.executable, "-c", IMPORT_THE_TASKS],
        cwd=PROJECT_ROOT,
        env={**env, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path / "missing")},
        capture_output=True,
        text=True,
    )
    existing = subprocess.run(
        [sys.executable, "-c", IMPORT_THE_TASKS],
        cwd=PROJECT_ROOT,
        env={**env, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
    )

    assert missing.returncode != 0
    assert "FileNotFoundError" in missing.stderr
    assert existing.returncode == 0, existing.stderr


def test_the_server_never_deletes_metric_files(tmp_path: Path) -> None:
    (tmp_path / "counter_77.db").write_bytes(b"a running process may have it open")
    port = free_port()

    server = worker_metrics.start_metrics_server(str(tmp_path), port, address="127.0.0.1")
    assert server is not None
    server.shutdown()

    # Cleaning is the container command's job, BEFORE Python starts (see compose files)
    assert (tmp_path / "counter_77.db").exists()


def test_without_a_multiprocess_folder_there_is_no_server(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        server = worker_metrics.start_metrics_server("", free_port())

    # Tasks run in CHILD processes: one process's own numbers would be almost empty
    assert server is None
    assert "PROMETHEUS_MULTIPROC_DIR" in caplog.text


def test_the_worker_starts_its_server_from_its_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    started: list[tuple[str, int]] = []
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    monkeypatch.setattr(
        worker_metrics, "start_metrics_server", lambda folder, port: started.append((folder, port))
    )

    worker_metrics.serve_worker_metrics()

    assert started == [(str(tmp_path), worker_metrics.get_settings().worker_metrics_port)]


def test_signals_without_a_task_id_are_counted_but_not_timed() -> None:
    unknown = tasks_total("unknown", task="unknown")
    timed = sample("celery_task_duration_seconds_count", task="unknown")

    worker_metrics.remember_start(task_id=None)  # nothing to remember
    worker_metrics.count_task(task_id=None, task=None, state=None)

    assert tasks_total("unknown", task="unknown") == unknown + 1
    assert sample("celery_task_duration_seconds_count", task="unknown") == timed  # no start time
