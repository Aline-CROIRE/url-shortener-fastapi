"""Write-behind clicks: the redirect hands each click to Celery and never writes it itself."""

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.dependencies import get_click_tracker
from app.core.config import Settings
from app.main import create_app
from app.services.clicks import ClickEvent
from app.worker.celery_app import celery_app, create_celery_app
from app.worker.tasks import track_click_task
from app.worker.tracker import CeleryClickTracker
from tests.test_click_task import saved, seed_url

API = "/api/v1/urls"
TARGET = "https://www.python.org/"


def create(client: TestClient) -> str:
    response = client.post(API, json={"original_url": TARGET})
    assert response.status_code == 201, response.text
    return response.json()["short_code"]


# ---------- the redirect only hands the click over ----------


def test_redirect_hands_over_the_click_with_simple_values(
    db_client: TestClient, owner_id: int, recorded_clicks: list[ClickEvent]
) -> None:
    code = create(db_client)

    before = datetime.now(UTC)
    response = db_client.get(
        f"/{code}", headers={"User-Agent": "Mozilla/5.0", "Referer": "https://x.com/post"}
    )

    assert response.status_code == 302
    [click] = recorded_clicks
    assert click.user_agent == "Mozilla/5.0"
    assert click.referrer == "https://x.com/post"
    assert click.ip_address is None  # TestClient's host "testclient" is not an IP
    assert click.clicked_at is not None
    assert before <= click.clicked_at <= datetime.now(UTC)
    url_id = db_client.get(f"{API}/{code}").json()["id"]
    assert click.url_id == url_id


def test_redirect_never_writes_to_the_database(
    db_client: TestClient,
    owner_id: int,
    query_counter: Callable,
    recorded_clicks: list[ClickEvent],
) -> None:
    code = create(db_client)

    with query_counter() as visit:
        db_client.get(f"/{code}")

    # Only the SELECT that finds the link: no INSERT click, no UPDATE click_count
    assert visit.count == 1, visit.statements
    assert visit.statements[0].lstrip().upper().startswith("SELECT")
    assert db_client.get(f"{API}/{code}").json()["click_count"] == 0  # the worker counts


def test_dead_links_hand_over_no_click(
    db_client: TestClient, owner_id: int, recorded_clicks: list[ClickEvent]
) -> None:
    code = create(db_client)
    db_client.put(f"{API}/{code}", json={"is_active": False})

    assert db_client.get(f"/{code}").status_code == 410
    assert db_client.get("/nothing").status_code == 404
    assert recorded_clicks == []


def test_tracker_problem_never_breaks_the_redirect(
    db_client: TestClient, owner_id: int, app: FastAPI
) -> None:
    class BrokenTask:
        def delay(self, *args: Any) -> None:
            raise ConnectionError("broker is down")

    app.dependency_overrides[get_click_tracker] = lambda: CeleryClickTracker(BrokenTask())
    code = create(db_client)

    response = db_client.get(f"/{code}")

    assert response.status_code == 302
    assert response.headers["location"] == TARGET


# ---------- the Celery tracker ----------


@pytest.mark.anyio
async def test_tracker_runs_the_task_in_eager_mode(worker_db: Session, celery_eager: None) -> None:
    url = seed_url(worker_db)

    await CeleryClickTracker(track_click_task).track(ClickEvent(url.id))

    assert saved(worker_db, url)[0] == 1


@pytest.mark.anyio
async def test_dead_broker_is_logged_and_fails_fast(caplog: pytest.LogCaptureFixture) -> None:
    dead = create_celery_app("redis://127.0.0.1:1/1")  # nothing listens on port 1

    @dead.task(name="tests.dead_broker")
    def task(*args: Any) -> None:
        return None

    start = time.perf_counter()
    with caplog.at_level(logging.WARNING):
        await CeleryClickTracker(task).track(ClickEvent(42))  # does not raise

    assert time.perf_counter() - start < 5  # no endless reconnecting
    assert "Click for url_id=42 not queued" in caplog.text


def test_api_fails_fast_when_the_broker_is_down() -> None:
    options = celery_app.conf.broker_transport_options

    assert celery_app.conf.task_publish_retry is False  # one try per click, no waiting
    assert options["max_retries"] == 0  # do not reconnect again and again
    assert options["socket_connect_timeout"] <= 1


def test_the_worker_finds_the_tasks() -> None:
    assert "app.worker.tasks" in celery_app.conf.include
    assert track_click_task.name in celery_app.tasks


def test_lifespan_creates_the_celery_tracker(test_settings: Settings) -> None:
    app = create_app(test_settings)

    with TestClient(app):
        assert isinstance(app.state.click_tracker, CeleryClickTracker)
        request = SimpleNamespace(app=app)  # all the dependency reads from the request
        assert get_click_tracker(request) is app.state.click_tracker  # type: ignore[arg-type]
