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
        "Short code does not exist",
        "url_not_found",
        "Short URL 'aB3dE9x' was not found.",
        {"short_code": "aB3dE9x"},
    ),
}
VALIDATION_ERROR: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: _error_doc(
        "Invalid input (bad URL in the body, or badly formed short code in the path)",
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
        "Unexpected server error (details are only written to the server log)",
        "internal_server_error",
        "An unexpected error occurred.",
    ),
}
STORAGE_UNAVAILABLE: dict[int | str, dict[str, Any]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: _error_doc(
        "Storage (Redis) is not reachable",
        "storage_unavailable",
        "The storage service is temporarily unavailable. Please try again later.",
    ),
}
