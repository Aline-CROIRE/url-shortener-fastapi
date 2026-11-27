"""Prometheus metrics of the Celery worker, served on its own port (WORKER_METRICS_PORT).

The API's /metrics shows requests; this shows what happens AFTER the response:
tasks run (and retried or failed), clicks saved or dropped, URLs archived.

Celery's prefork pool runs tasks in CHILD processes, so this is multiprocess mode
again (like Gunicorn): the children write their numbers to files in
PROMETHEUS_MULTIPROC_DIR, and the main process serves the sum on the metrics port.
That variable must be set in the container (compose, Kubernetes), not in code: the
children are copies of the main process and inherit its already-imported client.
"""

import logging
import os
import time
from pathlib import Path
from typing import Any

from celery.signals import task_postrun, task_prerun, worker_ready
from prometheus_client import CollectorRegistry, Counter, Histogram, start_http_server
from prometheus_client.multiprocess import MultiProcessCollector

from app.core.config import get_settings

logger = logging.getLogger(__name__)

TASKS = Counter(
    "celery_tasks_total",
    "Celery task runs by task name and outcome (success, retry, failure).",
    ["task", "outcome"],
)
TASK_DURATION = Histogram(
    "celery_task_duration_seconds",
    "Time one task run took (a retry is a new run).",
    ["task"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
CLICKS_SAVED = Counter(
    "clicks_saved_total",
    "Clicks the worker handled: saved, or dropped because the URL no longer exists.",
    ["outcome"],
)
URLS_ARCHIVED = Counter("urls_archived_total", "Expired URLs switched off by the nightly archive.")

# Start time of each running task, by task id (one entry per task, removed at the end)
_started: dict[str, float] = {}


@task_prerun.connect
def remember_start(task_id: str | None = None, **kwargs: Any) -> None:
    """Note when a task run starts."""
    if task_id is not None:
        _started[task_id] = time.perf_counter()


@task_postrun.connect
def count_task(
    task_id: str | None = None, task: Any = None, state: str | None = None, **kwargs: Any
) -> None:
    """Count the run with its outcome (Celery's state: SUCCESS, RETRY, FAILURE) and time it."""
    name = getattr(task, "name", "unknown")
    TASKS.labels(task=name, outcome=(state or "unknown").lower()).inc()
    start = _started.pop(task_id, None) if task_id is not None else None
    if start is not None:
        TASK_DURATION.labels(task=name).observe(time.perf_counter() - start)


def start_metrics_server(folder: str, port: int, address: str = "0.0.0.0") -> Any:
    """Serve the sum of every process's metrics from `folder` on `port`; None if no folder.

    Never deletes metric files: this process already has its own open. The container
    command empties and creates the folder BEFORE Python starts (compose, Kubernetes),
    because prometheus_client writes its files as soon as a metric is defined.
    """
    if not folder:
        logger.warning(
            "Worker metrics off: set PROMETHEUS_MULTIPROC_DIR (tasks run in child processes)"
        )
        return None
    Path(folder).mkdir(parents=True, exist_ok=True)
    registry = CollectorRegistry()
    MultiProcessCollector(registry, path=folder)
    server, _thread = start_http_server(port, addr=address, registry=registry)
    logger.info("Worker metrics on port %d", port)
    return server


@worker_ready.connect
def serve_worker_metrics(**kwargs: Any) -> None:
    """Start the metrics server in the worker's MAIN process, once it is ready.

    worker_ready (not worker_init): by then Celery has set up logging, so the "metrics
    on port ..." line reaches the log. Only the main process serves; the pool's
    children only write their files.
    """
    start_metrics_server(
        os.environ.get("PROMETHEUS_MULTIPROC_DIR", ""), get_settings().worker_metrics_port
    )
