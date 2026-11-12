from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(database_url: str, echo: bool = False) -> AsyncEngine:
    """Create the engine: the app's one shared connection pool to PostgreSQL."""
    return create_async_engine(
        database_url,
        echo=echo,  # True = log every SQL statement
        pool_pre_ping=True,  # test a pooled connection before use (survives DB restarts)
    )


def create_read_engine(primary: AsyncEngine, replica_url: str, echo: bool = False) -> AsyncEngine:
    """The engine for READ-ONLY queries.

    - With a replica URL: its own pool, connected to the replica.
    - Without: a copy of the primary engine that SHARES its pool (no extra connections).
    Either way every transaction is READ ONLY, so a write sent down the read path
    fails at once ("cannot execute INSERT in a read-only transaction").
    """
    base = create_engine(replica_url, echo=echo) if replica_url else primary
    return base.execution_options(postgresql_readonly=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create the sessionmaker: a factory that makes a new session per request."""
    return async_sessionmaker(
        bind=engine,
        # Keep loaded values usable after commit. In async code, reloading them
        # later would need an "await" that FastAPI's serializer cannot do.
        expire_on_commit=False,
    )


@asynccontextmanager
async def _session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Lend a session; roll back if the block fails; always close it.

    A context manager (not a plain generator + "async for"): "async with" passes
    an exception from the route INTO this function, so the rollback really runs.
    """
    async with session_factory() as session:  # "async with" closes the session at the end
        try:
            yield session  # the route runs here
        except Exception:
            await session.rollback()  # undo half-finished work if the request failed
            raise


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: the WRITE session (primary database).

    The engines and sessionmakers are created once in the app lifespan (app/main.py).
    """
    async with _session_scope(request.app.state.session_factory) as session:
        yield session


async def get_read_db(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: the READ-ONLY session (replica if configured, else primary).

    Use it for endpoints that only read. It may be slightly behind the primary
    (replication lag), so never read your own just-written data through it.
    """
    async with _session_scope(request.app.state.read_session_factory) as session:
        yield session


async def ping_database(session: AsyncSession) -> None:
    """Run the smallest possible query; raises if PostgreSQL does not answer."""
    await session.execute(text("SELECT 1"))
