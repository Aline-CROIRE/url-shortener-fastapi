"""Gunicorn settings for production (read by: gunicorn -c gunicorn.conf.py app.main:app).

Gunicorn is the process manager: it starts several worker processes, restarts any
that crash or hang, and replaces them one by one on a deploy. Each worker runs
Uvicorn, which speaks ASGI to FastAPI. More processes = more CPU cores used.
"""

import os
from pathlib import Path

# Prometheus multiprocess mode: every worker writes its numbers to files in this
# folder, and /metrics adds them up (app/core/metrics.py). Set HERE, in the master,
# before any worker starts: prometheus_client reads it when it is first imported.
PROMETHEUS_DIR = os.environ.setdefault("PROMETHEUS_MULTIPROC_DIR", "/tmp/prometheus-multiproc")

from app.core.capacity import check_connection_budget, default_web_workers
from app.core.config import get_settings
from app.core.logging import logging_config

settings = get_settings()

bind = "0.0.0.0:8000"
# WEB_CONCURRENCY if set, else 2 x CPU cores + 1
workers = settings.web_concurrency or default_web_workers()
worker_class = "uvicorn_worker.UvicornWorker"  # ASGI (async) workers, not WSGI
# No "threads": that setting is for Gunicorn's sync/gthread workers and a UvicornWorker
# ignores it. One worker serves many requests at once on its event loop; blocking calls
# use AnyIO's thread pool (THREAD_POOL_SIZE), password hashing its own pool of 4.

# Every worker has its own database pool: refuse to start if they could need
# more connections than PostgreSQL allows (see app/core/capacity.py)
try:
    db_connections = check_connection_budget(settings, workers)
except ValueError as exc:
    raise SystemExit(f"gunicorn.conf.py: {exc}") from exc

timeout = 30  # a worker silent for 30 s is killed and replaced (stuck, not just slow)
graceful_timeout = 30  # on shutdown/deploy: time to finish open requests
keepalive = 5  # seconds an idle client connection stays open (behind a proxy)

# Replace each worker after ~10,000 requests (jitter: not all at the same moment), a
# safety net so a slow memory leak can never grow without limit. Rare on purpose:
# - the profile (app/scripts/profile_redirects.py) showed no leak;
# - a recycled worker closes its keep-alive connections, and a client reusing one at
#   that moment gets "connection reset" (load test with 1000: 8 of 3709 requests).
max_requests = 10_000
max_requests_jitter = 1_000

# Gunicorn 26's runtime control socket (for "gunicornc") is not used: Docker and the
# orchestrator manage the processes with signals. Off = one admin interface less
# (and no error: the app user has no home folder to put the socket in).
control_socket_disable = True

# Logs: Gunicorn's own messages in the same JSON format as the app; no access log
# (our RequestLoggingMiddleware writes a richer one with the request id)
logconfig_dict = logging_config(settings.debug, settings.log_format)
accesslog = None
loglevel = "info"


def when_ready(server: object) -> None:
    """Log the sizing once, when Gunicorn has started (useful when reading logs)."""
    import logging

    logging.getLogger("gunicorn.error").info(
        "Gunicorn ready: %d workers, up to %d database connections (max_connections %d)",
        workers,
        db_connections,
        settings.db_max_connections,
    )


def on_starting(server: object) -> None:
    """Start every count from zero: remove metric files of an earlier run (master, once)."""
    folder = Path(PROMETHEUS_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    for stale in folder.glob("*.db"):  # only prometheus_client's files, nothing else
        stale.unlink()


def child_exit(server: object, worker: object) -> None:
    """Drop a stopped worker's "live" values (e.g. its requests in progress).

    Its counters stay: what it counted still happened.
    """
    from prometheus_client import multiprocess

    multiprocess.mark_process_dead(worker.pid, PROMETHEUS_DIR)  # type: ignore[attr-defined]
