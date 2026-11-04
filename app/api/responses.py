from typing import Any

from fastapi import status

from app.schemas.error import ErrorResponse

# Reusable OpenAPI docs for error responses, so Swagger shows our real error shape
NOT_FOUND: dict[int | str, dict[str, Any]] = {
    status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "Short code does not exist"},
}
VALIDATION_ERROR: dict[int | str, dict[str, Any]] = {
    status.HTTP_422_UNPROCESSABLE_CONTENT: {"model": ErrorResponse, "description": "Invalid input"},
}
STORAGE_UNAVAILABLE: dict[int | str, dict[str, Any]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": ErrorResponse,
        "description": "Storage (Redis) is not reachable",
    },
}
