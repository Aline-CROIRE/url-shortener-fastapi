"""Gunicorn settings for production (read by: gunicorn -c gunicorn.conf.py app.main:app).

Gunicorn is the process manager: it starts several worker processes, restarts any
that crash or hang, and replaces them one by one on a deploy. Each worker runs
Uvicorn, which speaks ASGI to FastAPI. More processes = more CPU cores used.
"""

from app.core.capacity import check_connection_budget, default_web_workers
from app.core.config import get_settings
from app.core.logging import logging_config

settings = get_settings()

bind = "0.0.0.0:8000"
# WEB_CONCURRENCY if set, else 2 x CPU cores + 1
workers = settings.web_concurrency or default_web_workers()
worker_class = "uvicorn_worker.UvicornWorker"  # ASGI (async) workers, not WSGI

# Every worker has its own database pool: refuse to start if they could need
# more connections than PostgreSQL allows (see app/core/capacity.py)
try:
    db_connections = check_connection_budget(settings, workers)
except ValueError as exc:
    raise SystemExit(f"gunicorn.conf.py: {exc}") from exc

timeout = 30  # a worker silent for 30 s is killed and replaced (stuck, not just slow)
graceful_timeout = 30  # on shutdown/deploy: time to finish open requests
keepalive = 5  # seconds an idle client connection stays open (behind a proxy)

# Replace each worker after ~1000 requests (jitter: not all at the same moment),
# so a slow memory leak can never grow without limit
max_requests = 1000
max_requests_jitter = 100

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
