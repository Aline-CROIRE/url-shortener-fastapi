"""The API side of click tracking: put each click on the Celery queue."""

import logging
from functools import partial
from typing import Any

import anyio

from app.core.metrics import CLICK_EVENTS
from app.services.clicks import ClickEvent

logger = logging.getLogger(__name__)


class CeleryClickTracker:
    """Sends each click to the worker with track_click_task.delay(...)."""

    def __init__(self, task: Any) -> None:
        """Keep the Celery task to send (track_click_task in production)."""
        self._task = task

    async def track(self, event: ClickEvent) -> None:
        """Queue the click; if that fails, log it and go on (the click is lost).

        .delay() is a normal blocking call (it talks to Redis), so it runs in a
        worker THREAD: the event loop keeps serving other requests meanwhile.
        """
        try:
            await anyio.to_thread.run_sync(partial(self._task.delay, *event.task_args()))
        except Exception:
            # Any failure (broker down, timeout, ...): a lost click must never
            # break a redirect. Logged with the stack trace to find the cause.
            logger.warning(
                "Click for url_id=%s not queued (is the broker down?)", event.url_id, exc_info=True
            )
            CLICK_EVENTS.labels("lost").inc()
        else:
            CLICK_EVENTS.labels("queued").inc()
