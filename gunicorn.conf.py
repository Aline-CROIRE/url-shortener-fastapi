"""Gunicorn settings for production.

Gunicorn is the process manager: it starts several worker processes and restarts
them if they crash. Each worker runs Uvicorn, which speaks ASGI to FastAPI.
"""

from app.core.config import get_settings

settings = get_settings()

bind = "0.0.0.0:8000"
workers = settings.workers  # from the WORKERS environment variable
worker_class = "uvicorn_worker.UvicornWorker"

timeout = 30  # restart a worker that is stuck for 30 seconds
graceful_timeout = 30  # time to finish open requests on shutdown
keepalive = 5

# Our RequestLoggingMiddleware already logs every request, so no access log here
accesslog = None
errorlog = "-"  # Gunicorn's own messages go to the console (stderr)
loglevel = "info"
