"""OpenTelemetry tracing: one trace per request (API -> DB -> queue -> worker), linked to logs.

Spans are collected in memory here; in production they go to an OTLP endpoint (Jaeger,
Tempo, Honeycomb, ...). Tracing is OFF unless OTEL_EXPORTER_OTLP_ENDPOINT is set.
"""

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
from celery import Celery
from celery.signals import before_task_publish
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.celery import CeleryInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError

from app.core import tracing
from app.core.config import Settings
from app.core.logging import JsonFormatter, RequestContextFilter
from app.main import create_app

ENDPOINT = "http://jaeger:4318"


def uninstrument_everything() -> None:
    """Instrumentors are process-wide: undo them so no other test is traced."""
    for instrumentor in (SQLAlchemyInstrumentor(), RedisInstrumentor(), CeleryInstrumentor()):
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
    tracing.reset_tracing()


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    """Turn tracing on with an in-memory exporter (instead of sending over the network)."""
    exporter = InMemorySpanExporter()
    monkeypatch.setattr(tracing, "OTLPSpanExporter", lambda **kwargs: exporter)
    monkeypatch.setattr(tracing, "BatchSpanProcessor", SimpleSpanProcessor)  # export at once
    # The global provider can be set only once per process: tests never touch it
    monkeypatch.setattr(tracing.trace, "set_tracer_provider", lambda provider: None)
    yield exporter
    uninstrument_everything()


@pytest.fixture
def traced_client(test_settings: Settings, spans: InMemorySpanExporter) -> Iterator[TestClient]:
    settings = test_settings.model_copy(update={"otel_exporter_otlp_endpoint": ENDPOINT})
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        yield client


def by_name(spans: InMemorySpanExporter) -> dict[str, ReadableSpan]:
    return {span.name: span for span in spans.get_finished_spans()}


# ---------- off by default ----------


def test_tracing_is_off_without_an_endpoint(test_settings: Settings) -> None:
    assert tracing.setup_tracing(test_settings, "api") is None

    app = create_app(test_settings)
    with TestClient(app):
        assert app.state.tracer_provider is None


def test_sample_ratio_must_be_between_0_and_1() -> None:
    with pytest.raises(ValidationError, match="otel_traces_sampler_ratio"):
        Settings(otel_traces_sampler_ratio=1.5)


# ---------- one trace per request ----------


def test_a_request_becomes_a_trace_with_its_database_query(
    traced_client: TestClient, spans: InMemorySpanExporter
) -> None:
    response = traced_client.get("/nosuch1")  # unknown code: one SELECT, then 404

    assert response.status_code == 404
    named = by_name(spans)
    # Named after the route TEMPLATE, like the metrics: never one name per short code
    server = named["GET /{short_code}"]
    query = named["SELECT"]  # (a "connect" span appears too when a new connection opens)
    assert query.context.trace_id == server.context.trace_id  # the same trace
    # SQL with placeholders: the values (here the short code) are never in the span
    assert "nosuch1" not in str(query.attributes.get("db.statement"))
    assert server.resource.attributes["service.name"] == "url-shortener-api"
    # No internal ASGI "http send/receive" spans: three per response, they hide the real steps
    assert not [s for s in spans.get_finished_spans() if " http " in s.name]


def test_health_and_metrics_are_not_traced(
    traced_client: TestClient, spans: InMemorySpanExporter
) -> None:
    traced_client.get("/health/live")
    traced_client.get("/metrics")

    # Probes and scrapes run every few seconds: tracing them would drown real traffic
    assert all(
        "health" not in s.name and "metrics" not in s.name for s in spans.get_finished_spans()
    )


def test_log_lines_carry_the_trace_id(
    traced_client: TestClient, spans: InMemorySpanExporter
) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RequestContextFilter())
    handler.setFormatter(JsonFormatter())
    logging.getLogger().addHandler(handler)
    try:
        traced_client.get("/nosuch2")
    finally:
        logging.getLogger().removeHandler(handler)

    [access] = [
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if json.loads(line)["logger"] == "app.requests"
    ]
    server = by_name(spans)["GET /{short_code}"]
    # From a log line straight to its trace (and back): the same id in both
    assert access["trace_id"] == format(server.context.trace_id, "032x")
    assert len(access["span_id"]) == 16


def test_without_a_span_log_lines_have_no_trace_fields() -> None:
    record = logging.makeLogRecord({"msg": "outside any request"})
    RequestContextFilter().filter(record)

    line = json.loads(JsonFormatter().format(record))

    assert "trace_id" not in line
    assert "span_id" not in line


def test_spans_are_flushed_when_the_app_stops(
    test_settings: Settings, spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = test_settings.model_copy(update={"otel_exporter_otlp_endpoint": ENDPOINT})
    app = create_app(settings)  # the provider is created with the app
    flushed: list[bool] = []
    monkeypatch.setattr(app.state.tracer_provider, "shutdown", lambda: flushed.append(True))

    with TestClient(app):
        pass  # startup, then shutdown

    # Spans wait in a batch in memory: without this, the last ones would be lost
    assert flushed == [True]


# ---------- the trace continues into the Celery worker ----------


def test_the_trace_id_travels_inside_the_task_message(spans: InMemorySpanExporter) -> None:
    provider = tracing.setup_tracing(Settings(otel_exporter_otlp_endpoint=ENDPOINT), "api")
    assert provider is not None
    CeleryInstrumentor().instrument(tracer_provider=provider)
    queue = Celery("trace-test", broker="memory://")  # an in-process broker

    @queue.task(name="trace_test.echo")
    def echo() -> None:
        return None

    sent_headers: list[dict[str, Any]] = []

    def remember(headers: dict[str, Any] | None = None, **kwargs: Any) -> None:
        sent_headers.append(headers or {})

    before_task_publish.connect(remember, weak=False)
    try:
        with provider.get_tracer("test").start_as_current_span("GET /{short_code}") as span:
            echo.delay()
            trace_id = format(span.get_span_context().trace_id, "032x")
    finally:
        before_task_publish.disconnect(remember)

    [sent] = sent_headers
    # W3C "traceparent": version-traceid-spanid-flags. The worker continues THIS trace.
    assert trace_id in sent["traceparent"]


def test_the_worker_starts_tracing_in_each_process(
    spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.worker import celery_app as celery_module

    monkeypatch.setattr(
        celery_module, "get_settings", lambda: Settings(otel_exporter_otlp_endpoint=ENDPOINT)
    )

    celery_module.start_worker_tracing()

    assert CeleryInstrumentor().is_instrumented_by_opentelemetry
    assert tracing.current_provider() is not None
    provider = tracing.current_provider()
    assert provider is not None
    assert provider.resource.attributes["service.name"] == "url-shortener-worker"


def test_the_worker_does_nothing_without_an_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.worker import celery_app as celery_module

    monkeypatch.setattr(celery_module, "get_settings", lambda: Settings())

    celery_module.start_worker_tracing()

    assert not CeleryInstrumentor().is_instrumented_by_opentelemetry
    assert tracing.current_provider() is None


def test_the_worker_flushes_its_spans_when_a_process_stops(
    spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.worker import celery_app as celery_module

    monkeypatch.setattr(
        celery_module, "get_settings", lambda: Settings(otel_exporter_otlp_endpoint=ENDPOINT)
    )
    celery_module.start_worker_tracing()
    provider = tracing.current_provider()
    assert provider is not None
    flushed: list[bool] = []
    monkeypatch.setattr(provider, "shutdown", lambda: flushed.append(True))

    celery_module.stop_worker_tracing()

    assert flushed == [True]


def test_stopping_a_worker_without_tracing_does_nothing() -> None:
    from app.worker import celery_app as celery_module

    celery_module.stop_worker_tracing()  # no provider: must not fail

    assert tracing.current_provider() is None
