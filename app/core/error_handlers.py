"""Turn exceptions into JSON error responses that all share one shape."""

import logging
from collections.abc import Awaitable, Callable, Mapping
from http import HTTPStatus
from typing import Any, cast

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ExceptionHandler

from app.core.client import client_ip
from app.core.exceptions import AppError, ConflictError
from app.core.logging import clean_for_log
from app.core.middleware import SECURITY_HEADERS
from app.db.errors import is_retryable, unique_violation_constraint

logger = logging.getLogger(__name__)
security_logger = logging.getLogger("app.security")


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Build the one error shape: {"error": {"code", "message", "details"}}."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details}},
        headers=headers,
    )


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """Return the JSON error for one of our own AppError exceptions.

    Every 403 is also logged: many of them in a row can mean someone is probing
    other users' URLs. The line says who, what and from where; never the token
    or any header.
    """
    if exc.status_code == status.HTTP_403_FORBIDDEN:
        security_logger.warning(
            "403 %s on %s %s user_id=%s ip=%s",
            exc.code,
            request.method,
            clean_for_log(request.url.path),
            getattr(request.state, "user_id", "-"),
            client_ip(request),
        )
    return error_response(exc.status_code, exc.code, exc.message, exc.details, exc.headers)


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Return HTTP errors (like 404 or 405) in our JSON error shape."""
    # Also catches FastAPI's own 404 (unknown route) and 405 (wrong method)
    phrase = HTTPStatus(exc.status_code).phrase  # e.g. "Not Found"
    code = phrase.lower().replace(" ", "_").replace("-", "_")  # e.g. "not_found"
    message = exc.detail if isinstance(exc.detail, str) else phrase
    return error_response(exc.status_code, code, message, headers=exc.headers)


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return validation errors as a 422 with one entry per bad field."""
    details = [
        {
            "field": ".".join(str(part) for part in error["loc"]),
            "message": error["msg"],
            "type": error["type"],
        }
        for error in exc.errors()
    ]
    return error_response(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        "validation_error",
        "The request data is invalid.",
        details,
    )


async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    """Turn a duplicate unique value into a clean 409.

    Any other constraint error is a bug, so it becomes a 500.
    """
    # Thanks to our naming convention the name is readable, e.g. "uq_users_email"
    constraint = unique_violation_constraint(exc)
    if constraint is None:
        return await unhandled_exception_handler(request, exc)
    conflict = ConflictError(details={"constraint": constraint})
    return await app_error_handler(request, conflict)


async def database_error_handler(request: Request, exc: DBAPIError) -> JSONResponse:
    """Turn a deadlock or serialization failure into a 503 the client can retry.

    Postgres rolled the whole transaction back, so nothing was half-saved and the
    same request can be sent again. Any other database error is a 500.
    """
    if not is_retryable(exc):
        return await unhandled_exception_handler(request, exc)
    logger.warning(
        "Retryable database conflict on %s %s", request.method, clean_for_log(request.url.path)
    )
    return error_response(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "try_again",
        "The server was busy with a conflicting request. Please try again.",
        headers={"Retry-After": "1"},
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log an unexpected error and return a general 500 without details."""
    # Full stack trace goes to the server log only, never to the client
    logger.exception("Unhandled error on %s %s", request.method, clean_for_log(request.url.path))
    # A 500 is answered by Starlette's outermost error layer, past our middlewares,
    # so the security headers (and the request id) are added here by hand
    headers = dict(SECURITY_HEADERS)
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        headers["X-Request-ID"] = request_id
    return error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "internal_server_error",
        "An unexpected error occurred.",
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register all error handlers on the app."""
    handlers: list[tuple[type[Exception], Callable[..., Awaitable[JSONResponse]]]] = [
        (AppError, app_error_handler),
        (StarletteHTTPException, http_exception_handler),
        (RequestValidationError, validation_exception_handler),
        (IntegrityError, integrity_error_handler),
        # other database errors (IntegrityError, a subclass, keeps its own handler)
        (DBAPIError, database_error_handler),
        (Exception, unhandled_exception_handler),
    ]
    for exception_class, handler in handlers:
        # Starlette types a handler as accepting ANY Exception; ours accept the
        # specific class they are registered for, which is what Starlette passes.
        app.add_exception_handler(exception_class, cast(ExceptionHandler, handler))
