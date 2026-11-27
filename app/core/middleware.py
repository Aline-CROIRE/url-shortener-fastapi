"""Middleware: request logging with a request ID, a body size limit, and security headers."""

import logging
import re
import time
import uuid

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import bind_request_context, clean_for_log

logger = logging.getLogger("app.requests")

REQUEST_ID_HEADER = "X-Request-ID"
# A request id from the caller is kept only if it is plain text of a sane length;
# anything else (spaces, control characters, other bytes) gets a fresh id
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}")
# The longest path written into the request log
MAX_LOGGED_PATH = 200


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Logs every request and adds an X-Request-ID header to every response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Log the request and add the X-Request-ID header to the response."""
        # Reuse the caller's ID if it is a plain one, so logs can be matched across
        # services; otherwise make a new one.
        incoming_id = request.headers.get(REQUEST_ID_HEADER, "")
        is_plain = REQUEST_ID_PATTERN.fullmatch(incoming_id) is not None
        request_id = incoming_id if is_plain else uuid.uuid4().hex
        # request.state = data that belongs to THIS request only
        request.state.request_id = request_id
        # From here on, EVERY log line of this request carries its id, method and path
        bind_request_context(request_id, request.method, request.url.path)

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # The global 500 handler builds the response; we still log the request
            self._log(request, 500, start, request_id)
            raise

        response.headers[REQUEST_ID_HEADER] = request_id
        self._log(request, response.status_code, start, request_id)
        return response

    @staticmethod
    def _log(request: Request, status_code: int, start: float, request_id: str) -> None:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.info(
            "%s %s %s",
            request.method,
            # The path is the client's text: clean it, or escape codes reach the log
            clean_for_log(request.url.path, MAX_LOGGED_PATH),
            status_code,
            # Separate JSON fields, so "status >= 500" or "duration_ms > 500" is a filter
            extra={"status": status_code, "duration_ms": duration_ms, "request_id": request_id},
        )


# The largest request body accepted. The biggest real body (a 2048-character URL,
# 10 tags and a 2000-character description) is well under 10 KB.
MAX_BODY_BYTES = 64 * 1024


def payload_too_large() -> JSONResponse:
    """Return the 413 error in our usual JSON error shape."""
    return JSONResponse(
        status_code=413,
        content={
            "error": {
                "code": "payload_too_large",
                "message": f"The request body is larger than {MAX_BODY_BYTES} bytes.",
                "details": None,
            }
        },
    )


class BodySizeLimitMiddleware:
    """Refuse request bodies larger than MAX_BODY_BYTES with 413 (a plain ASGI middleware).

    Without a limit, a client can send megabytes that are parsed, validated and
    even stored. A too-large Content-Length is refused at once; a body sent in
    chunks (no Content-Length) is counted while it arrives and refused as soon
    as it passes the limit, so at most MAX_BODY_BYTES is ever held in memory.
    """

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the next ASGI app."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Check the body size, then pass the request on with its (small) body."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            await payload_too_large()(scope, receive, send)
            return

        # Read the whole body first (it is small, or refused), then hand it on
        body = b""
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] != "http.request":  # the client went away
                await self.app(scope, self._replay([message], receive), send)
                return
            body += message.get("body", b"")
            if len(body) > MAX_BODY_BYTES:
                await payload_too_large()(scope, receive, send)
                return
            more_body = message.get("more_body", False)

        request = {"type": "http.request", "body": body, "more_body": False}
        await self.app(scope, self._replay([request], receive), send)

    @staticmethod
    def _replay(messages: list[Message], receive: Receive) -> Receive:
        """Give the app the messages already read, then the client's later ones."""
        pending = list(messages)

        async def replay() -> Message:
            if pending:
                return pending.pop(0)
            return await receive()

        return replay


# Sent with every response. nosniff: browsers must trust our Content-Type and never
# "guess" that a JSON body is HTML or a script. DENY: no other site may show our
# pages inside a frame (clickjacking).
SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"}


class SecurityHeadersMiddleware:
    """Add SECURITY_HEADERS to every HTTP response (a plain ASGI middleware: no extra copy)."""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the next ASGI app."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Pass the request on, and add the headers when the response starts."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)
