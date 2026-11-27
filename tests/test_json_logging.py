"""Structured JSON logs: one JSON object per line, with the request's id on every line."""

import io
import json
import logging
import sys
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

import pytest
from celery.signals import setup_logging as celery_setup_logging
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.core.logging import (
    JsonFormatter,
    RequestContextFilter,
    TextFormatter,
    redact,
    setup_logging,
)

JsonLines = Callable[[], list[dict[str, Any]]]
SPEC_FIELDS = {"timestamp", "level", "logger", "message", "request_id", "path", "method"}
PASSWORD = "Sup3r-Secret-Passw0rd!"


@pytest.fixture
def json_logs() -> Iterator[JsonLines]:
    """Everything logged during the test, as our JSON handler writes it."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RequestContextFilter())
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    yield lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
    root.removeHandler(handler)
    root.setLevel(old_level)


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Put the normal logging setup back after a test that reconfigures it."""
    yield
    setup_logging(debug=False, log_format="json", force=True)


# ---------- the format ----------


def test_a_log_line_is_one_json_object_with_the_spec_fields(json_logs: JsonLines) -> None:
    logging.getLogger("app.example").warning("hello %s", "world")

    [line] = json_logs()
    assert set(line) >= SPEC_FIELDS
    assert line["level"] == "WARNING"
    assert line["logger"] == "app.example"
    assert line["message"] == "hello world"
    assert line["timestamp"].endswith("Z")  # UTC, ISO 8601
    datetime.fromisoformat(line["timestamp"])  # readable by any tool
    assert line["request_id"] is None  # not inside a request


def test_a_line_break_cannot_start_a_fake_log_line(json_logs: JsonLines) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("app.injection")
    logger.addHandler(handler)
    try:
        logger.warning('user said: hi\n{"level": "INFO", "message": "fake"}')
    finally:
        logger.removeHandler(handler)

    output = stream.getvalue()
    assert output.count("\n") == 1  # one record = one line: JSON escapes the break
    assert json.loads(output)["message"].startswith("user said: hi\n")


# ---------- request context ----------


def test_access_line_has_id_path_method_status_and_duration(
    client: TestClient, json_logs: JsonLines
) -> None:
    response = client.get("/api/v1/no-such-page", headers={"X-Request-ID": "trace-abc-123"})

    assert response.headers["X-Request-ID"] == "trace-abc-123"
    [access] = [line for line in json_logs() if line["logger"] == "app.requests"]
    assert access["request_id"] == "trace-abc-123"
    assert access["method"] == "GET"
    assert access["path"] == "/api/v1/no-such-page"
    assert access["status"] == 404
    assert isinstance(access["duration_ms"], float)
    assert access["duration_ms"] >= 0


def test_lines_written_inside_a_request_carry_its_id(
    db_client: TestClient, json_logs: JsonLines
) -> None:
    response = db_client.post(
        "/api/v1/auth/login", data={"username": "nobody@example.com", "password": PASSWORD}
    )

    request_id = response.headers["X-Request-ID"]
    security = [line for line in json_logs() if line["logger"] == "app.security"]
    assert security, "the failed login must be logged"
    # Written deep inside the auth service, yet it knows which request it belongs to
    assert all(line["request_id"] == request_id for line in security)
    assert all(line["path"] == "/api/v1/auth/login" for line in security)


def test_a_500_is_logged_with_the_stack_trace_and_the_request_id(
    app: FastAPI, client: TestClient, json_logs: JsonLines
) -> None:
    async def boom() -> None:
        raise RuntimeError("kaboom")

    app.add_api_route("/api/v1/boom-for-test", boom)

    response = client.get("/api/v1/boom-for-test")

    assert response.status_code == 500
    [error] = [line for line in json_logs() if line["level"] == "ERROR"]
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert "Traceback" in error["exception"]
    assert "RuntimeError: kaboom" in error["exception"]
    assert "kaboom" not in response.text  # the client still sees nothing internal


def test_each_request_gets_its_own_id(client: TestClient, json_logs: JsonLines) -> None:
    client.get("/api/v1/first")
    client.get("/api/v1/second")

    ids = [line["request_id"] for line in json_logs() if line["logger"] == "app.requests"]
    assert len(ids) == 2
    assert ids[0] != ids[1]


# ---------- secrets never reach the log ----------


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI3In0.abc_DEF-123 used", "eyJhbGciOiJIUzI1NiJ9"),
        ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
        ("login password=Hunter2! for x", "Hunter2!"),
        ('body {"password": "Hunter2!", "email": "a@b.c"}', "Hunter2!"),
        ("refresh_token=opaque-refresh-value-123", "opaque-refresh-value-123"),
    ],
)
def test_secrets_are_redacted(text: str, secret: str) -> None:
    cleaned = redact(text)

    assert secret not in cleaned
    assert "[REDACTED]" in cleaned


def test_secrets_in_a_stack_trace_are_redacted_too(json_logs: JsonLines) -> None:
    token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiI3In0.c2lnbmF0dXJl"
    try:
        raise ValueError(f"could not decode {token}")
    except ValueError:
        logging.getLogger("app.example").exception("decode failed")

    [line] = json_logs()
    assert token not in json.dumps(line)


def test_a_full_login_session_never_logs_the_password_or_tokens(
    db_client: TestClient, json_logs: JsonLines
) -> None:
    email = "logcheck@example.com"
    db_client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    db_client.post("/api/v1/auth/login", data={"username": email, "password": "Wrong-Passw0rd!"})
    tokens = db_client.post(
        "/api/v1/auth/login", data={"username": email, "password": PASSWORD}
    ).json()
    access, refresh = tokens["access_token"], tokens["refresh_token"]
    db_client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access}"})
    db_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
    db_client.post("/api/v1/auth/logout", json={"refresh_token": refresh})

    everything = json.dumps(json_logs())
    assert PASSWORD not in everything
    assert "Wrong-Passw0rd!" not in everything
    assert access not in everything
    assert refresh not in everything
    assert email not in everything  # only masked: "l***@example.com"


# ---------- the setup (dictConfig) ----------


def test_setup_sends_json_to_stdout_once(restore_logging: None) -> None:
    setup_logging(debug=False, log_format="json", force=True)
    setup_logging(debug=False, log_format="json", force=True)  # twice: still one handler

    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    [handler] = handlers
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stdout  # containers collect stdout
    assert isinstance(handler.formatter, JsonFormatter)


def test_setup_runs_once_unless_forced(restore_logging: None) -> None:
    setup_logging(debug=False, log_format="json", force=True)

    setup_logging(debug=True, log_format="text")  # e.g. a second create_app(): ignored

    assert isinstance(logging.getLogger().handlers[0].formatter, JsonFormatter)


def test_text_format_for_reading_by_eye(restore_logging: None) -> None:
    setup_logging(debug=True, log_format="text", force=True)

    [handler] = logging.getLogger().handlers
    assert isinstance(handler.formatter, TextFormatter)
    assert logging.getLogger("app").level == logging.DEBUG


def test_server_logs_go_through_the_same_handler(restore_logging: None) -> None:
    setup_logging(debug=False, log_format="json", force=True)

    # Uvicorn/Gunicorn messages become JSON too; their access log stays off
    # (our middleware writes a richer one)
    for name in ("uvicorn", "uvicorn.error", "gunicorn.error"):
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate is True
    assert logging.getLogger("uvicorn.access").level >= logging.WARNING


def test_celery_worker_uses_the_same_json_logging(restore_logging: None) -> None:
    import app.worker.celery_app  # noqa: F401  (connects the signal)

    # A receiver on setup_logging tells Celery NOT to configure logging itself
    assert celery_setup_logging.receivers
    celery_setup_logging.send(sender=None, loglevel="INFO", logfile=None, format="", colorize=False)

    [handler] = logging.getLogger().handlers
    assert isinstance(handler.formatter, JsonFormatter)


def test_log_format_setting_is_checked() -> None:
    with pytest.raises(ValidationError, match="log_format"):
        Settings(log_format="xml")  # type: ignore[arg-type]


def test_text_lines_show_the_request_id_and_hide_secrets() -> None:
    record = logging.makeLogRecord(
        {"name": "app.example", "levelname": "INFO", "msg": "used Bearer abc.def.ghi"}
    )

    line = TextFormatter().format(record)

    assert "[None]" in line  # no request: the id box is empty, the layout stays the same
    assert "abc.def.ghi" not in line
    assert "[REDACTED]" in line
    record.request_id = "req-7"
    assert "[req-7]" in TextFormatter().format(record)


def test_values_passed_with_extra_win_over_the_request_context() -> None:
    record = logging.makeLogRecord(
        {"msg": "x", "request_id": "from-extra", "method": "PUT", "path": "/given"}
    )

    RequestContextFilter().filter(record)

    assert (record.request_id, record.method, record.path) == ("from-extra", "PUT", "/given")
