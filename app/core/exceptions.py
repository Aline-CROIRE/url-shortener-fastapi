"""Errors the app raises on purpose; each one becomes a JSON error response."""

from typing import Any


class AppError(Exception):
    """Base class for errors we expect and want to show to the client.

    Each subclass sets its HTTP status code and a short machine-readable code.
    """

    status_code: int = 500
    code: str = "app_error"
    message: str = "An application error occurred."

    def __init__(self, message: str | None = None, details: Any = None) -> None:
        """Store the error message and extra details.

        Args:
            message: Text for the client. None keeps the class default.
            details: Extra data sent in the "details" field of the error.
        """
        self.message = message or self.message
        self.details = details
        super().__init__(self.message)


class UrlNotFoundError(AppError):
    """Raised when a short URL cannot be found (404 Not Found)."""

    status_code = 404
    code = "url_not_found"

    def __init__(self, short_code: str) -> None:
        """Build the error for the given short code."""
        super().__init__(
            message=f"Short URL '{short_code}' was not found.",
            details={"short_code": short_code},
        )


class UrlGoneError(AppError):
    """The short URL exists but must not redirect any more (410 Gone)."""

    status_code = 410

    def __init__(self, short_code: str, reason: str) -> None:
        """Build the error for the given short code.

        Args:
            short_code: The short code that was asked for.
            reason: Why it no longer redirects, "expired" or "inactive".
                It becomes part of the error code.
        """
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


class AliasTakenError(ConflictError):
    """Raised when a custom alias is already used (409 Conflict)."""

    code = "alias_taken"

    def __init__(self, alias: str) -> None:
        """Build the error for the given alias."""
        super().__init__(
            message=f"The alias '{alias}' is already taken. Please choose another one.",
            details={"custom_alias": alias},
        )


class InvalidCursorError(AppError):
    """The pagination cursor is broken, forged or from another sort order (400)."""

    status_code = 400
    code = "invalid_cursor"
    message = "The cursor is not valid. Start again without a cursor."


class NotAuthenticatedError(AppError):
    """Raised when an endpoint needs a logged-in user and there is none (401)."""

    status_code = 401
    code = "not_authenticated"
    message = "Log in first: POST /api/v1/auth/login/."


class InvalidCredentialsError(AppError):
    """Raised when login fails; the same message for an unknown email or a wrong password (401)."""

    status_code = 401
    code = "invalid_credentials"
    message = "The email or password is wrong."


class ShortCodeGenerationError(AppError):
    """Raised when no unique short code could be generated (500 Internal Server Error)."""

    status_code = 500
    code = "short_code_generation_failed"
    message = "Could not generate a unique short code. Please try again."
