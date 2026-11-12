from typing import Any


class AppError(Exception):
    """Base class for errors we expect and want to show to the client.

    Each subclass sets its HTTP status code and a short machine-readable code.
    """

    status_code: int = 500
    code: str = "app_error"
    message: str = "An application error occurred."

    def __init__(self, message: str | None = None, details: Any = None) -> None:
        self.message = message or self.message
        self.details = details
        super().__init__(self.message)


class UrlNotFoundError(AppError):
    status_code = 404
    code = "url_not_found"

    def __init__(self, short_code: str) -> None:
        super().__init__(
            message=f"Short URL '{short_code}' was not found.",
            details={"short_code": short_code},
        )


class UrlGoneError(AppError):
    """The short URL exists but must not redirect any more (410 Gone)."""

    status_code = 410

    def __init__(self, short_code: str, reason: str) -> None:
        self.code = f"url_{reason}"  # "url_expired" or "url_inactive"
        super().__init__(
            message=f"Short URL '{short_code}' is {reason} and no longer redirects.",
            details={"short_code": short_code},
        )


class ConflictError(AppError):
    """A unique value is already taken (409 Conflict)."""

    status_code = 409
    code = "conflict"
    message = "This value already exists."


class ShortCodeGenerationError(AppError):
    status_code = 500
    code = "short_code_generation_failed"
    message = "Could not generate a unique short code. Please try again."


class StorageUnavailableError(AppError):
    status_code = 503
    code = "storage_unavailable"
    message = "The storage service is temporarily unavailable. Please try again later."
