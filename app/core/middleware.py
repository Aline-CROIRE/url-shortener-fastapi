"""Middleware: request logging with a request ID, and security headers on every response."""

import logging
import time
import uuid

from fastapi import Request, Response
from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("app.requests")

REQUEST_ID_HEADER = "X-Request-ID"


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Logs every request and adds an X-Request-ID header to every response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """Log the request and add the X-Request-ID header to the response."""
        # Reuse the caller's ID if it sent a short one, so logs can be matched
        # across services; otherwise make a new one.
        incoming_id = request.headers.get(REQUEST_ID_HEADER, "")
        request_id = incoming_id if 0 < len(incoming_id) <= 128 else uuid.uuid4().hex
        # request.state = data that belongs to THIS request only
        request.state.request_id = request_id

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
        duration_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "%s %s %s %.2fms request_id=%s",
            request.method,
            request.url.path,
            status_code,
            duration_ms,
            request_id,
        )


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
