"""Prometheus metrics: what the app counts and measures, and the /metrics output.

Prometheus "scrapes" (reads) GET /metrics every few seconds and keeps the numbers as
time series. From them come graphs and alerts, e.g. "95% of redirects take under
20 ms" or "clicks are being lost: alert!".

METRIC TYPES
- Counter:   only goes up (requests served, clicks lost). Graphs use its rate.
- Histogram: counts values into buckets (request durations) -> percentiles (p95, p99).
- Gauge:     goes up and down (requests running right now).

LABELS ARE CHOSEN WITH CARE: each label combination is a separate time series.
The route TEMPLATE ("/{short_code}"), never the real path ("/abc1234"): one series per
short code would mean millions of series (a "cardinality explosion") and would leak
the codes into the monitoring system.

SEVERAL GUNICORN WORKERS: each is its own process with its own counters. With
PROMETHEUS_MULTIPROC_DIR set (gunicorn.conf.py does it), every worker writes its
numbers to files in that folder, and /metrics adds them up, so one scrape sees the
whole server and not one random worker.
"""

import os

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.multiprocess import MultiProcessCollector

# ---------- HTTP (written by MetricsMiddleware) ----------

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests answered, by method, route template and status code.",
    ["method", "route", "status"],
)
HTTP_DURATION = Histogram(
    "http_request_duration_seconds",
    "Time from receiving a request to the end of its response, by route template.",
    ["method", "route"],
    # Redirects take milliseconds; logins take ~0.3 s (password hashing on purpose)
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
HTTP_IN_PROGRESS = Gauge(
    "http_requests_in_progress",
    "Requests being handled right now.",
    ["method"],
    # Several workers: add up the values of the workers that are alive
    multiprocess_mode="livesum",
)

# ---------- business metrics ----------

CACHE_LOOKUPS = Counter(
    "redirect_cache_lookups_total",
    "Redirect cache lookups: hit, miss, or error (Redis down: the database answers).",
    ["result"],
)
CLICK_EVENTS = Counter(
    "click_events_total",
    "Clicks handed to Celery: queued, or lost because the broker could not be reached.",
    ["outcome"],
)

# Methods we expect; anything else is counted as "other" (a client cannot invent labels)
KNOWN_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
UNMATCHED_ROUTE = "unmatched"


def method_label(method: str) -> str:
    """Return the method, or "other" for a method we never serve."""
    return method if method in KNOWN_METHODS else "other"


def render_metrics(multiprocess_dir: str | None = None) -> tuple[bytes, str]:
    """Return the metrics in Prometheus' text format, and its content type.

    With a multiprocess folder (several Gunicorn workers), the numbers of every
    worker are read from there and added up; otherwise this process's own.
    """
    folder = multiprocess_dir or os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if folder:
        registry = CollectorRegistry()
        MultiProcessCollector(registry, path=folder)
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
