"""OpenTelemetry tracing: follow ONE request through every part of the system.

A TRACE is the whole journey of one request; each step in it is a SPAN (a name, a
start, a duration, its parent). For a redirect:

    GET /{short_code}                       (FastAPI, in the API)
    |-- SELECT urls ...                     (SQLAlchemy -> PostgreSQL)
    |-- GET url:redirect:...                (Redis)
    `-- apply_async/clicks.track_click      (Celery: the message carries the trace id)
        `-- run/clicks.track_click          (in the WORKER, a moment later)
            `-- INSERT INTO clicks ...      (SQLAlchemy -> PostgreSQL)

Logs say what happened, metrics say how often; traces say WHERE the time went.
Every JSON log line written inside a span gets its trace_id, so one click in the
tracing UI shows the logs of that exact request (and the other way round).

OFF unless OTEL_EXPORTER_OTLP_ENDPOINT is set (e.g. http://jaeger:4318): no spans are
recorded or sent, so it costs nothing.
"""

from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.celery import CeleryInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from sqlalchemy import Engine

from app.core.config import Settings

# Probes and scrapes run every few seconds: tracing them would drown real traffic
EXCLUDED_URLS = "health,metrics"
# Give up sending a batch after 5 s: a slow tracing backend must not pile up work
EXPORT_TIMEOUT_SECONDS = 5

# This process's provider (one per process: the API worker or the Celery worker)
_provider: TracerProvider | None = None


def current_provider() -> TracerProvider | None:
    """Return this process's tracer provider, or None when tracing is off."""
    return _provider


def reset_tracing() -> None:
    """Forget the provider (tests; a real process sets it up once)."""
    global _provider
    _provider = None


def setup_tracing(settings: Settings, component: str) -> TracerProvider | None:
    """Create the tracer provider for this process, or return None when tracing is off.

    Args:
        settings: Endpoint, service name, sample ratio, version, environment.
        component: "api" or "worker": becomes part of the service name in the UI.
    """
    global _provider
    if not settings.otel_exporter_otlp_endpoint:
        return None
    resource = Resource.create(
        {
            SERVICE_NAME: f"{settings.otel_service_name}-{component}",
            SERVICE_VERSION: settings.app_version,
            "deployment.environment.name": settings.environment,
        }
    )
    provider = TracerProvider(
        resource=resource,
        # Keep this share of NEW traces (1.0 = all). ParentBased: a request that
        # arrives with a sampled trace (e.g. the task from a traced API request)
        # is always kept, so a trace is never cut in half.
        sampler=ParentBased(TraceIdRatioBased(settings.otel_traces_sampler_ratio)),
    )
    exporter = OTLPSpanExporter(
        endpoint=f"{settings.otel_exporter_otlp_endpoint.rstrip('/')}/v1/traces",
        timeout=EXPORT_TIMEOUT_SECONDS,
    )
    # Batch: spans are sent in the background, in groups, never during a request
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)  # for any code that asks the global API
    _provider = provider
    return provider


def instrument_api(app: Any, provider: TracerProvider) -> None:
    """Trace incoming requests, Redis calls and the Celery tasks the API sends."""
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls=EXCLUDED_URLS,
        # No spans for the ASGI "receive"/"send" steps: noise, not real work
        exclude_spans=["receive", "send"],
    )
    RedisInstrumentor().instrument(tracer_provider=provider)
    # Sending side: puts the trace id into each task message ("traceparent" header)
    CeleryInstrumentor().instrument(tracer_provider=provider)


def instrument_worker(provider: TracerProvider) -> None:
    """Trace the tasks the worker runs (continuing the API's trace) and its Redis calls."""
    CeleryInstrumentor().instrument(tracer_provider=provider)
    RedisInstrumentor().instrument(tracer_provider=provider)


def instrument_engines(*engines: Engine) -> None:
    """Trace every SQL statement of these engines (no-op when tracing is off).

    Spans hold the SQL with placeholders ($1), never the values sent with it.
    """
    if _provider is None:
        return
    SQLAlchemyInstrumentor().instrument(engines=list(engines), tracer_provider=_provider)
