"""Helpers for tests that run two REAL database connections at the same moment."""

import asyncio
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

WAIT_SECONDS = 10


async def count_lock_waiters(engine: AsyncEngine) -> int:
    """Return how many connections of the test database are waiting for a lock."""
    async with engine.connect() as connection:
        # AUTOCOMMIT: Postgres takes ONE snapshot of pg_stat_activity per transaction,
        # so inside one transaction a watcher would never see a new waiter appear
        watcher = await connection.execution_options(isolation_level="AUTOCOMMIT")
        waiting = await watcher.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE wait_event_type = 'Lock' AND datname = current_database()"
            )
        )
    return int(waiting or 0)


async def wait_until_blocked(engine: AsyncEngine, racing: asyncio.Task[Any]) -> None:
    """Return once the racing task is blocked on a row lock.

    No fixed sleep, so the test is not flaky on a slow machine. If the racing task
    ends before it ever waited, its own result (or real error) is raised at once.
    """
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if racing.done():
            await racing  # raises the task's real error, if it had one
            raise AssertionError("the second request finished without waiting for the lock")
        if await count_lock_waiters(engine):
            return
        await asyncio.sleep(0.02)
    racing.cancel()
    raise AssertionError("the second request never waited for the first")
