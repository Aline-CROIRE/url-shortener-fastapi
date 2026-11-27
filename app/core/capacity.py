"""Production sizing: how many Gunicorn workers, and do their database connections fit?

THE CONNECTION MATH
Every Gunicorn worker is a separate process with its OWN connection pool, and
every Celery worker process has its own too. At the busiest moment:

    web workers x (DB_POOL_SIZE + DB_MAX_OVERFLOW)
  + Celery processes x connections per Celery process
  + a reserve (admin psql, migrations, monitoring, PostgreSQL's superuser slots)
  must stay BELOW PostgreSQL's max_connections (100 by default).

Example: 4 workers x (3 + 2) + 2 x 2 + 10 = 34  (fits in 100)
         40 workers x (3 + 2) + 2 x 2 + 10 = 214 (PostgreSQL refuses connections)
"""

import os

from app.core.config import Settings
from app.worker.db import WORKER_MAX_OVERFLOW, WORKER_POOL_SIZE

# Kept free for people and tools (psql, Alembic, monitoring) and the 3 slots
# PostgreSQL keeps for superusers (superuser_reserved_connections)
RESERVED_CONNECTIONS = 10
CELERY_CONNECTIONS_PER_PROCESS = WORKER_POOL_SIZE + WORKER_MAX_OVERFLOW


def default_web_workers(cpu_count: int | None = None) -> int:
    """Return the classic Gunicorn default: 2 x CPU cores + 1.

    While one worker waits (database, Redis), another can use the core.
    WEB_CONCURRENCY overrides it.
    """
    cores = cpu_count if cpu_count is not None else os.cpu_count()
    return 2 * (cores or 1) + 1


def connections_needed(
    web_workers: int,
    pool_size: int,
    max_overflow: int,
    celery_processes: int,
    celery_per_process: int = CELERY_CONNECTIONS_PER_PROCESS,
) -> int:
    """Return the most database connections the app can open at the same time."""
    return web_workers * (pool_size + max_overflow) + celery_processes * celery_per_process


def check_connection_budget(settings: Settings, web_workers: int) -> int:
    """Return the connections needed, or refuse a setup that would exceed max_connections.

    Raises:
        ValueError: If the workers could open more connections than PostgreSQL allows.
    """
    needed = connections_needed(
        web_workers, settings.db_pool_size, settings.db_max_overflow, settings.celery_concurrency
    )
    budget = settings.db_max_connections - RESERVED_CONNECTIONS
    if needed > budget:
        raise ValueError(
            f"{web_workers} web workers x ({settings.db_pool_size} + {settings.db_max_overflow}) "
            f"+ {settings.celery_concurrency} Celery processes = {needed} database connections, "
            f"but PostgreSQL max_connections {settings.db_max_connections} - "
            f"{RESERVED_CONNECTIONS} reserved = {budget}. Lower WEB_CONCURRENCY or DB_POOL_SIZE."
        )
    return needed
