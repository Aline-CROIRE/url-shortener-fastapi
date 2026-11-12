import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import DateTime, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import DbSession
from app.db.base import Base, TimestampMixin
from app.db.database import get_db


def add_test_route(app: FastAPI) -> None:
    """A tiny route that asks the database which database it is connected to."""

    @app.get("/_test/db-name")
    async def db_name(db: DbSession) -> dict[str, str]:
        name = (await db.execute(text("SELECT current_database()"))).scalar_one()
        return {"database": name}


def test_get_db_uses_the_separate_test_database(app: FastAPI, db_client: TestClient) -> None:
    add_test_route(app)

    response = db_client.get("/_test/db-name")

    assert response.status_code == 200
    assert response.json() == {"database": "url_shortener_test"}


def test_get_db_is_overridden_in_tests(app: FastAPI, db_client: TestClient) -> None:
    assert get_db in app.dependency_overrides


def test_test_transaction_is_rolled_back(app: FastAPI, db_client: TestClient) -> None:
    # Create a temporary table inside the test transaction; the rollback removes it
    @app.post("/_test/make-table")
    async def make_table(db: DbSession) -> dict[str, bool]:
        await db.execute(text("CREATE TABLE rollback_probe (id int)"))
        await db.commit()  # only releases a savepoint: the outer transaction stays open
        return {"ok": True}

    assert db_client.post("/_test/make-table").status_code == 200


def test_rolled_back_table_does_not_exist(app: FastAPI, db_client: TestClient) -> None:
    # Runs after the test above: if its rollback worked, the table is gone
    @app.get("/_test/probe-exists")
    async def probe_exists(db: DbSession) -> dict[str, bool]:
        found = (await db.execute(text("SELECT to_regclass('rollback_probe')"))).scalar_one()
        return {"exists": found is not None}

    assert db_client.get("/_test/probe-exists").json() == {"exists": False}


def test_get_db_closes_the_session_after_the_request(client: TestClient) -> None:
    # Use the real get_db (no override) and track the session it hands out
    sessions = []

    @client.app.get("/_test/session")
    async def grab_session(db: DbSession) -> dict[str, bool]:
        sessions.append(db)
        await db.execute(text("SELECT 1"))
        return {"in_transaction": db.in_transaction()}

    assert client.get("/_test/session").json() == {"in_transaction": True}
    # After the response, get_db's "async with" closed the session: no open transaction
    assert sessions[0].in_transaction() is False


def test_timestamp_mixin_adds_id_created_at_updated_at() -> None:
    class Probe(TimestampMixin, Base):
        __tablename__ = "probe_mixin"

    columns = Probe.__table__.columns
    assert columns["id"].primary_key
    for name in ("created_at", "updated_at"):
        assert isinstance(columns[name].type, DateTime)
        assert columns[name].type.timezone is True
        assert columns[name].server_default is not None
    assert columns["updated_at"].onupdate is not None

    # Clean up: remove the probe table from the shared metadata catalogue
    Base.metadata.remove(Probe.__table__)


def test_get_db_rolls_back_when_the_request_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The REAL get_db (no override): a crash after using the session must roll back
    rollbacks: list[AsyncSession] = []
    original_rollback = AsyncSession.rollback

    async def spy_rollback(self: AsyncSession) -> None:
        rollbacks.append(self)
        await original_rollback(self)

    monkeypatch.setattr(AsyncSession, "rollback", spy_rollback)

    @client.app.get("/_test/crash")
    async def crash(db: DbSession) -> None:
        await db.execute(text("SELECT 1"))
        raise RuntimeError("something broke halfway")

    response = client.get("/_test/crash")

    assert response.status_code == 500
    assert len(rollbacks) == 1  # get_db's "except: rollback" ran
