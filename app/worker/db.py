"""The Celery worker's database connection: SYNCHRONOUS SQLAlchemy with psycopg.

Celery runs a task as a normal (not async) function, so the worker uses a normal
Session instead of the API's AsyncSession. Same database, same models and SQL;
only the driver differs (psycopg instead of asyncpg).
"""

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

# Each worker PROCESS runs one task at a time, so it needs one connection,
# plus one spare (e.g. while a broken connection is replaced)
WORKER_POOL_SIZE = 1
WORKER_MAX_OVERFLOW = 1


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    """Create the worker's engine (connection pool) and sessionmaker, once per process.

    Created on the FIRST task, not at import: Celery starts the worker processes
    by copying ("forking") the main one, and a connection copied into two
    processes would be used by both at the same time.
    """
    engine = create_engine(
        get_settings().sync_database_url,
        pool_size=WORKER_POOL_SIZE,
        max_overflow=WORKER_MAX_OVERFLOW,
        pool_pre_ping=True,  # test a pooled connection first (survives DB restarts)
    )
    return sessionmaker(engine, expire_on_commit=False)
