"""/health/startup: has this instance finished starting (database reachable, schema current)?"""

import asyncio
import time

import pytest
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient

from app.api.routes import health as health_routes
from app.db.migrations import MIGRATIONS_DIR, expected_schema_head


def test_started_when_the_schema_is_at_the_newest_migration(client: TestClient) -> None:
    response = client.get("/health/startup")  # the test database is migrated to head

    assert response.status_code == 200
    assert response.json() == {"status": "started", "database_schema": "current"}
    assert response.headers["cache-control"] == "no-store"


def test_an_old_schema_means_still_starting(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def old_revision(session: object) -> str:
        return "0123456789ab"  # e.g. the new image runs before the migration job finished

    monkeypatch.setattr(health_routes, "current_schema_revision", old_revision)

    response = client.get("/health/startup")

    # 503: Kubernetes keeps waiting (and sends no traffic) instead of serving
    # requests with code that expects columns that do not exist yet
    assert response.status_code == 503
    assert response.json() == {"status": "starting", "database_schema": "outdated"}


def test_an_unreachable_database_means_still_starting(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(session: object) -> str:
        raise ConnectionRefusedError("PostgreSQL is starting")

    monkeypatch.setattr(health_routes, "current_schema_revision", down)

    response = client.get("/health/startup")

    assert response.status_code == 503
    assert response.json() == {"status": "starting", "database_schema": "down"}


def test_a_hanging_database_fails_the_startup_check_quickly(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def hangs(session: object) -> str:
        await asyncio.sleep(30)
        return "never"

    monkeypatch.setattr(health_routes, "current_schema_revision", hangs)
    monkeypatch.setattr(health_routes, "CHECK_TIMEOUT_SECONDS", 0.2)

    start = time.perf_counter()
    response = client.get("/health/startup")

    assert response.status_code == 503
    assert time.perf_counter() - start < 5


def test_the_expected_head_is_the_single_newest_migration() -> None:
    heads = ScriptDirectory(str(MIGRATIONS_DIR)).get_heads()

    assert len(heads) == 1  # two heads = two migration branches nobody merged
    assert expected_schema_head() == heads[0]


def test_the_head_is_read_once() -> None:
    # Reading the migration files is disk work: done once per process, not per probe
    assert expected_schema_head() is expected_schema_head()


def test_a_project_without_migrations_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db import migrations

    class NoMigrations:
        def __init__(self, path: str) -> None:
            pass

        def get_current_head(self) -> None:
            return None

    monkeypatch.setattr(migrations, "ScriptDirectory", NoMigrations)
    migrations.expected_schema_head.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="No migrations found"):
            migrations.expected_schema_head()
    finally:
        migrations.expected_schema_head.cache_clear()


def test_a_database_without_migrations_is_reported_as_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import ProgrammingError

    async def no_table(session: object) -> str:
        # Seen in a real cluster: an empty database (lost its volume) answers fine,
        # but has no alembic_version table. "down" would send people to the network.
        raise ProgrammingError(
            "SELECT version_num FROM alembic_version", {}, Exception("UndefinedTableError")
        )

    monkeypatch.setattr(health_routes, "current_schema_revision", no_table)

    response = client.get("/health/startup")

    assert response.status_code == 503
    assert response.json() == {"status": "starting", "database_schema": "missing"}
