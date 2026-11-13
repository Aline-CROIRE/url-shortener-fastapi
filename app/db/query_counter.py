"""Count the SQL statements an engine sends (used by tests to catch N+1 queries).

with count_queries(engine) as counter:
...do something...
assert counter.count <= 3, counter.statements
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine

# Transaction bookkeeping is not "work" for the database: don't count it
IGNORED_PREFIXES = (
    "SAVEPOINT",
    "RELEASE SAVEPOINT",
    "ROLLBACK TO SAVEPOINT",
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
)


class QueryCounter:
    """Collect the SQL statements sent while counting is on."""

    def __init__(self) -> None:
        """Start with an empty list of statements."""
        self.statements: list[str] = []  # kept so a failing test can show the extra queries

    @property
    def count(self) -> int:
        """Return how many statements were counted."""
        return len(self.statements)

    def on_before_cursor_execute(
        self,
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        """Record one statement, unless it is transaction bookkeeping (BEGIN, COMMIT, ...)."""
        if not statement.lstrip().upper().startswith(IGNORED_PREFIXES):
            self.statements.append(statement)


@contextmanager
def count_queries(engine: AsyncEngine | Engine) -> Iterator[QueryCounter]:
    """Count the SQL statements the engine sends while the block runs.

    Listens to "before_cursor_execute" (fired just before each statement reaches
    the database driver) while the block runs, then stops listening.
    """
    # Events live on the synchronous core engine, also for an AsyncEngine
    sync_engine = engine.sync_engine if isinstance(engine, AsyncEngine) else engine
    counter = QueryCounter()
    event.listen(sync_engine, "before_cursor_execute", counter.on_before_cursor_execute)
    try:
        yield counter
    finally:
        event.remove(sync_engine, "before_cursor_execute", counter.on_before_cursor_execute)
