import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import AppError, ConflictError
from app.db.errors import unique_violation_constraint

logger = logging.getLogger(__name__)


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build the one error shape: {"error": {"code", "message", "details"}}."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details}},
        headers=headers,
    )


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return error_response(exc.status_code, exc.code, exc.message, exc.details)


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # Also catches FastAPI's own 404 (unknown route) and 405 (wrong method)
    phrase = HTTPStatus(exc.status_code).phrase  # e.g. "Not Found"
    code = phrase.lower().replace(" ", "_").replace("-", "_")  # e.g. "not_found"
    message = exc.detail if isinstance(exc.detail, str) else phrase
    return error_response(exc.status_code, code, message, headers=exc.headers)


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
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
    """Duplicate unique value -> clean 409. Any other constraint error is a bug -> 500."""
    # Thanks to our naming convention the name is readable, e.g. "uq_users_email"
    constraint = unique_violation_constraint(exc)
    if constraint is None:
        return await unhandled_exception_handler(request, exc)
    conflict = ConflictError(details={"constraint": constraint})
    return await app_error_handler(request, conflict)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Full stack trace goes to the server log only, never to the client
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    request_id = getattr(request.state, "request_id", None)
    return error_response(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "internal_server_error",
        "An unexpected error occurred.",
        headers={"X-Request-ID": request_id} if request_id else None,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(IntegrityError, integrity_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
