"""Prometheus metrics: request count and latency per ROUTE TEMPLATE, cache and click counters."""

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY
from pydantic import SecretStr

from app.core.metrics import render_metrics
from app.services.clicks import ClickEvent
from app.worker.tracker import CeleryClickTracker
from tests.test_redirect_cache import FakeRedis, create, fake_redis

__all__ = ["fake_redis"]  # the fixture, imported so pytest finds it in this file

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def sample(name: str, **labels: str) -> float:
    """The current value of one metric sample (0 if it was never recorded)."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


def requests_total(method: str, route: str, status: str) -> float:
    return sample("http_requests_total", method=method, route=route, status=status)


# ---------- the endpoint ----------


def test_metrics_endpoint_speaks_prometheus(client: TestClient) -> None:
    response = client.get("/metrics")

    assert response.status_code == 200
    # The text format Prometheus reads (prometheus-client 0.26: version 1.0.0)
    assert response.headers["content-type"] == CONTENT_TYPE_LATEST
    assert response.headers["cache-control"] == "no-store"
    assert "# TYPE http_requests_total counter" in response.text
    assert "# TYPE http_request_duration_seconds histogram" in response.text


def test_metrics_are_not_in_the_api_docs(client: TestClient) -> None:
    assert "/metrics" not in client.get("/openapi.json").json()["paths"]


def test_metrics_cannot_be_taken_as_a_custom_alias(db_client: TestClient, owner_id: int) -> None:
    response = db_client.post(
        "/api/v1/urls", json={"original_url": "https://www.python.org/", "custom_alias": "metrics"}
    )

    assert response.status_code == 422  # the link would be hidden behind GET /metrics


# ---------- labels: the route TEMPLATE, never the real path ----------


def test_a_redirect_is_counted_under_its_route_template(
    db_client: TestClient, owner_id: int, recorded_clicks: list[ClickEvent]
) -> None:
    code = create(db_client)
    before = requests_total("GET", "/{short_code}", "302")

    db_client.get(f"/{code}")

    assert requests_total("GET", "/{short_code}", "302") == before + 1
    # One series per ROUTE, not per short code: millions of codes would mean
    # millions of series (and the codes would leak into the monitoring system)
    assert code not in db_client.get("/metrics").text


def test_unknown_paths_share_one_label(client: TestClient) -> None:
    before = requests_total("GET", "unmatched", "404")

    client.get("/api/v1/random-scanner-path-1")
    client.get("/api/v1/random-scanner-path-2")

    assert requests_total("GET", "unmatched", "404") == before + 2
    assert "random-scanner-path" not in client.get("/metrics").text


def test_odd_http_methods_share_one_label(client: TestClient) -> None:
    before = requests_total("other", "unmatched", "404")

    client.request("FOOBAR", "/api/v1/whatever")

    assert requests_total("other", "unmatched", "404") >= before + 1
    assert "FOOBAR" not in client.get("/metrics").text


def test_latency_is_recorded_per_route(client: TestClient) -> None:
    labels = {"method": "GET", "route": "/health/live"}
    before = sample("http_request_duration_seconds_count", **labels)

    client.get("/health/live")

    assert sample("http_request_duration_seconds_count", **labels) == before + 1
    assert sample("http_request_duration_seconds_sum", **labels) > 0


def test_a_crash_is_counted_as_a_500(app: FastAPI, client: TestClient) -> None:
    async def boom() -> None:
        raise RuntimeError("kaboom")

    app.add_api_route("/api/v1/boom-metrics", boom)
    before = requests_total("GET", "/api/v1/boom-metrics", "500")

    assert client.get("/api/v1/boom-metrics").status_code == 500

    assert requests_total("GET", "/api/v1/boom-metrics", "500") == before + 1


def test_requests_in_progress_go_back_to_zero(client: TestClient) -> None:
    client.get("/health/live")

    assert sample("http_requests_in_progress", method="GET") == 0


# ---------- business metrics ----------


def test_cache_hits_and_misses_are_counted(
    db_client: TestClient,
    owner_id: int,
    fake_redis: FakeRedis,
    recorded_clicks: list[ClickEvent],
) -> None:
    code = create(db_client)
    hits = sample("redirect_cache_lookups_total", result="hit")
    misses = sample("redirect_cache_lookups_total", result="miss")

    db_client.get(f"/{code}")  # miss, then cached
    db_client.get(f"/{code}")  # hit

    assert sample("redirect_cache_lookups_total", result="miss") == misses + 1
    assert sample("redirect_cache_lookups_total", result="hit") == hits + 1


def test_redis_errors_are_counted(
    db_client: TestClient,
    owner_id: int,
    fake_redis: FakeRedis,
    recorded_clicks: list[ClickEvent],
) -> None:
    code = create(db_client)
    fake_redis.down = True
    errors = sample("redirect_cache_lookups_total", result="error")

    db_client.get(f"/{code}")

    assert sample("redirect_cache_lookups_total", result="error") == errors + 1


@pytest.mark.anyio
async def test_queued_and_lost_clicks_are_counted() -> None:
    class WorkingTask:
        def delay(self, *args: Any) -> None:
            return None

    class BrokenTask:
        def delay(self, *args: Any) -> None:
            raise ConnectionError("broker is down")

    queued = sample("click_events_total", outcome="queued")
    lost = sample("click_events_total", outcome="lost")

    await CeleryClickTracker(WorkingTask()).track(ClickEvent(1))
    await CeleryClickTracker(BrokenTask()).track(ClickEvent(1))

    assert sample("click_events_total", outcome="queued") == queued + 1
    # An alert on this counter catches a broken broker long before anyone looks at logs
    assert sample("click_events_total", outcome="lost") == lost + 1


# ---------- protecting the endpoint ----------


@pytest.fixture
def token_client(test_settings: Any) -> Any:
    from app.main import create_app

    settings = test_settings.model_copy(update={"metrics_token": SecretStr("scrape-secret-123")})
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        yield client


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "scrape-secret-123"}],
)
def test_with_a_token_metrics_need_it(token_client: TestClient, headers: dict[str, str]) -> None:
    response = token_client.get("/metrics", headers=headers)

    assert response.status_code == 401
    assert "http_requests_total" not in response.text


def test_with_the_right_token_metrics_are_shown(token_client: TestClient) -> None:
    response = token_client.get("/metrics", headers={"Authorization": "Bearer scrape-secret-123"})

    assert response.status_code == 200
    assert "http_requests_total" in response.text


# ---------- several Gunicorn workers: the numbers are added up ----------

COUNT_IN_A_NEW_PROCESS = """
from app.core.metrics import HTTP_REQUESTS
HTTP_REQUESTS.labels(method="GET", route="/{short_code}", status="302").inc()
"""


def test_multiprocess_mode_adds_up_every_worker(
    tmp_path: Path,
) -> None:
    env = {"PROMETHEUS_MULTIPROC_DIR": str(tmp_path), "JWT_SECRET_KEY": "k" * 32}
    # Two separate processes, like two Gunicorn workers, each count one redirect
    for _ in range(2):
        subprocess.run(
            [sys.executable, "-c", COUNT_IN_A_NEW_PROCESS],
            cwd=PROJECT_ROOT,
            env={**env, "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")},  # Windows needs it
            check=True,
        )

    body, _ = render_metrics(multiprocess_dir=str(tmp_path))

    line = 'http_requests_total{method="GET",route="/{short_code}",status="302"} 2.0'
    assert line in body.decode()  # one scrape sees BOTH workers


def test_an_empty_token_setting_means_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import Settings

    monkeypatch.setenv("METRICS_TOKEN", "")  # "METRICS_TOKEN=" copied from .env.example

    assert Settings().metrics_token is None  # not an empty secret that locks /metrics
