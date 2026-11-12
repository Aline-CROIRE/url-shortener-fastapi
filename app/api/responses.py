from typing import Any

from fastapi import status

from app.schemas.error import ErrorResponse

# Reusable OpenAPI docs for error responses, so Swagger shows our real error shape


def _error_doc(description: str, code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {
        "model": ErrorResponse,
        "description": description,
        "content": {
            "application/json": {
                "example": {"error": {"code": code, "message": message, "details": details}}
            }
        },
    }


NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: _error_doc(
        "Not found",
        "url_not_found",
        "Short URL 'aB3dE9x' was not found.",
        {"short_code": "aB3dE9x"},
    ),
}
GONE: dict[int | str, dict[str, Any]] = {
    status.HTTP_410_GONE: _error_doc(
        "Expired or switched off",
        "url_expired",
        "Short URL 'aB3dE9x' is expired and no longer redirects.",
        {"short_code": "aB3dE9x"},
    ),
}
CONFLICT: dict[int | str, dict[str, Any]] = {
    status.HTTP_409_CONFLICT: _error_doc(
        "Already exists",
        "conflict",
        "This value already exists.",
        {"constraint": "uq_urls_custom_alias"},
    ),
}
VALIDATION_ERROR: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: _error_doc(
        "Invalid input",
        "validation_error",
        "The request data is invalid.",
        [
            {
                "field": "body.original_url",
                "message": "Input should be a valid URL, relative URL without a base",
                "type": "url_parsing",
            }
        ],
    ),
}
INTERNAL_ERROR: dict[int | str, dict[str, Any]] = {
    status.HTTP_500_INTERNAL_SERVER_ERROR: _error_doc(
        "Server error",
        "internal_server_error",
        "An unexpected error occurred.",
    ),
}
STORAGE_UNAVAILABLE: dict[int | str, dict[str, Any]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: _error_doc(
        "Redis is down",
        "storage_unavailable",
        "The storage service is temporarily unavailable. Please try again later.",
    ),
}
