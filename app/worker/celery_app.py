"""The Celery app: the job queue shared by the API (sends jobs) and the worker (runs them).

Start the worker:  celery -A app.worker.celery_app:celery_app worker --loglevel=INFO
Start beat:        celery -A app.worker.celery_app:celery_app beat --loglevel=INFO
"""

from celery import Celery

from app.core.config import get_settings


def create_celery_app(broker_url: str) -> Celery:
    """Create the Celery app that sends jobs to (and reads them from) the broker."""
    celery = Celery("url_shortener", broker=broker_url)
    celery.conf.update(
        # JSON only: a "pickle" message could run any code inside the worker
        task_serializer="json",
        accept_content=["json"],
        # Fire and forget: nobody reads a task's return value, so none is stored
        task_ignore_result=True,
        # Times (and the nightly schedule) in UTC, like the database
        timezone="UTC",
        enable_utc=True,
        # The worker keeps trying to reach Redis at startup instead of crashing
        broker_connection_retry_on_startup=True,
    )
    return celery


celery_app = create_celery_app(get_settings().celery_broker_url)
