from collections.abc import AsyncIterator

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


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create the sessionmaker: a factory that makes a new session per request."""
    return async_sessionmaker(
        bind=engine,
        # Keep loaded values usable after commit. In async code, reloading them
        # later would need an "await" that FastAPI's serializer cannot do.
        expire_on_commit=False,
    )


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: lend one session to a request, then always close it.

    The engine and sessionmaker are created once in the app lifespan (app/main.py).
    """
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with session_factory() as session:  # "async with" closes the session at the end
        try:
            yield session  # the route runs here
        except Exception:
            await session.rollback()  # undo half-finished work if the request failed
            raise


async def ping_database(session: AsyncSession) -> None:
    """Run the smallest possible query; raises if PostgreSQL does not answer."""
    await session.execute(text("SELECT 1"))
