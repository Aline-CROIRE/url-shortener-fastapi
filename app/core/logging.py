"""Logging: JSON lines on stdout, the request's id on every line, secrets removed.

One log line = one JSON object, e.g.
{"timestamp": "2026-11-27T09:00:00.123Z", "level": "INFO", "logger": "app.requests",
 "message": "GET /abc1234 302", "request_id": "9f2c...", "path": "/abc1234",
 "method": "GET", "status": 302, "duration_ms": 3.2}

A log collector (Loki, CloudWatch, ELK, ...) reads each field without guessing,
so "every line of request 9f2c..." or "every 500 today" is a simple filter.
"""

import json
import logging
import logging.config
import re
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal

# Longest piece of client input written into one log line
MAX_LOGGED_LENGTH = 100

LogFormat = Literal["json", "text"]

# ---------- the request context ----------
# A ContextVar is a variable with one value PER REQUEST: two requests running at
# the same time each see their own id. The middleware sets them; every log line
# written while that request runs (route, service, repository) picks them up.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
request_method_var: ContextVar[str | None] = ContextVar("request_method", default=None)
request_path_var: ContextVar[str | None] = ContextVar("request_path", default=None)


def bind_request_context(request_id: str, method: str, path: str) -> None:
    """Remember this request's id, method and path for every log line it writes."""
    request_id_var.set(request_id)
    request_method_var.set(method)
    request_path_var.set(clean_for_log(path, 200))


class RequestContextFilter(logging.Filter):
    """Copy the request's id, method and path onto every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Add the request fields (a value passed with extra=... wins); keep the record."""
        if getattr(record, "request_id", None) is None:
            record.request_id = request_id_var.get()
        if getattr(record, "method", None) is None:
            record.method = request_method_var.get()
        if getattr(record, "path", None) is None:
            record.path = request_path_var.get()
        return True  # a filter could also DROP records; this one never does


# ---------- secrets never reach the log ----------
# A safety net: our code never logs secrets on purpose, but a library error
# message or a careless f-string could. Applied to every message and stack trace.
REDACTED = "[REDACTED]"
SECRET_PATTERNS = [
    # A JWT: three base64url parts separated by dots, the first starts with "eyJ"
    (re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+"), REDACTED),
    # "Bearer <anything>" from an Authorization header
    (re.compile(r"(Bearer\s+)[^\s\"',]+", re.IGNORECASE), rf"\1{REDACTED}"),
    # password=..., "password": "...", refresh_token=..., secret: ...
    (
        re.compile(
            r"(\b(?:password|passwd|refresh_token|access_token|secret|api_key)\b[\"']?\s*[:=]\s*)"
            r"(\"[^\"]*\"|'[^']*'|[^\s,&;\"']+)",
            re.IGNORECASE,
        ),
        rf"\1{REDACTED}",
    ),
]


def redact(text: str) -> str:
    """Replace anything that looks like a token or a password with [REDACTED]."""
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# ---------- formatters: a LogRecord -> one line of text ----------


class JsonFormatter(logging.Formatter):
    """Write each record as ONE line of JSON (json.dumps escapes any line break inside)."""

    def format(self, record: logging.LogRecord) -> str:
        """Build the JSON line: the spec's fields, plus extras when present."""
        line: dict[str, Any] = {
            # Milliseconds + "Z" (UTC): sortable, and the same zone on every server
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
            "request_id": getattr(record, "request_id", None),
            "path": getattr(record, "path", None),
            "method": getattr(record, "method", None),
        }
        # Only on the access line (see RequestLoggingMiddleware)
        for field in ("status", "duration_ms"):
            if hasattr(record, field):
                line[field] = getattr(record, field)
        if record.exc_info:
            # The full stack trace, as one string (still on one line once in JSON)
            line["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(line, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """A plain line for reading by eye (LOG_FORMAT=text); same redaction as JSON."""

    def __init__(self) -> None:
        """Use a short layout that still shows the request id."""
        super().__init__("%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        """Format the record, then remove secrets."""
        if not hasattr(record, "request_id"):
            record.request_id = None
        return redact(super().format(record))


# ---------- the setup ----------

_configured = False


def logging_config(debug: bool, log_format: LogFormat) -> dict[str, Any]:
    """Return the dictConfig: one stdout handler for everything, JSON or text."""
    return {
        "version": 1,
        # Keep loggers that modules already created (logging.getLogger(__name__))
        "disable_existing_loggers": False,
        "filters": {"request_context": {"()": RequestContextFilter}},
        "formatters": {"json": {"()": JsonFormatter}, "text": {"()": TextFormatter}},
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                # stdout, not a file: Docker and log collectors read it from there
                "stream": "ext://sys.stdout",
                "formatter": log_format,
                "filters": ["request_context"],
            }
        },
        "root": {"level": "INFO", "handlers": ["stdout"]},
        "loggers": {
            # More detail for our own code in debug mode, without noisy library logs
            "app": {"level": "DEBUG" if debug else "INFO"},
            # Servers: no handlers of their own, so their lines go to root -> JSON
            "uvicorn": {"handlers": [], "propagate": True},
            "uvicorn.error": {"handlers": [], "propagate": True},
            "gunicorn.error": {"handlers": [], "propagate": True},
            # Our RequestLoggingMiddleware already writes a richer access line
            "uvicorn.access": {"handlers": [], "level": "WARNING", "propagate": True},
            "gunicorn.access": {"handlers": [], "level": "WARNING", "propagate": True},
        },
    }


def setup_logging(debug: bool, log_format: LogFormat = "json", *, force: bool = False) -> None:
    """Configure logging for the whole process, once (force=True: again).

    Logging belongs to the process, not to one app object: a second create_app()
    (tests, scripts) must not replace the handlers already in place.
    """
    global _configured
    if _configured and not force:
        return
    logging.config.dictConfig(logging_config(debug, log_format))
    _configured = True


# ---------- helpers for logging client input safely ----------


def clean_for_log(text: str, max_length: int = MAX_LOGGED_LENGTH) -> str:
    """Make client input safe to write into a log line.

    Every character that is not printable (line breaks, tabs, terminal escape
    codes) becomes "?", so a client cannot start a fake log line ("log
    injection"). Long input is cut, so a client cannot flood the log.
    """
    cleaned = "".join(char if char.isprintable() else "?" for char in text[:max_length])
    return cleaned + "..." if len(text) > max_length else cleaned


def mask_email(email: str) -> str:
    """Keep only the first letter and the domain: "aline@x.com" -> "a***@x.com".

    Enough to spot a pattern (many failures for one domain or address) without
    writing a full personal email address into the logs.
    """
    local, at, domain = email.strip().rpartition("@")
    if not at or not local:
        return "***"
    return f"{clean_for_log(local[:1])}***@{clean_for_log(domain, 60)}"
