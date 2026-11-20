"""Errors the app raises on purpose; each one becomes a JSON error response."""

from collections.abc import Mapping
from typing import Any, ClassVar


class AppError(Exception):
    """Base class for errors we expect and want to show to the client.

    Each subclass sets its HTTP status code and a short machine-readable code.
    """

    status_code: int = 500
    code: str = "app_error"
    message: str = "An application error occurred."
    # Extra HTTP headers for the response (e.g. WWW-Authenticate on a 401).
    # ClassVar + Mapping: shared by the class and only read, never changed
    headers: ClassVar[Mapping[str, str] | None] = None

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


class RedirectLoopError(AppError):
    """The original URL points back to this shortener, so it would loop (422)."""

    status_code = 422
    code = "redirect_loop"

    def __init__(self, original_url: str) -> None:
        """Build the error for the refused original URL."""
        super().__init__(
            message=(
                "The original URL cannot point to this URL shortener itself: "
                "the short link would redirect to itself (or to another short link) in a loop."
            ),
            details={"original_url": original_url},
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
    message = "Log in first: POST /api/v1/auth/login/, then send 'Authorization: Bearer <token>'."
    # RFC 6750: a 401 tells the client which kind of login the server expects
    headers: ClassVar[Mapping[str, str]] = {"WWW-Authenticate": "Bearer"}


class InvalidCredentialsError(AppError):
    """Raised when login fails; the same message for an unknown email or a wrong password (401)."""

    status_code = 401
    code = "invalid_credentials"
    message = "The email or password is wrong."
    headers: ClassVar[Mapping[str, str]] = {"WWW-Authenticate": "Bearer"}


class InvalidTokenError(AppError):
    """Raised for any bad JWT, or a JWT whose user is gone or switched off (401).

    Bad means forged, expired, malformed or the wrong type. One message for every
    case, so an attacker learns nothing about why it failed.
    """

    status_code = 401
    code = "invalid_token"
    message = "The token is invalid or has expired."
    headers: ClassVar[Mapping[str, str]] = {"WWW-Authenticate": 'Bearer error="invalid_token"'}


class ForbiddenError(AppError):
    """Logged in, but not allowed to do this (403 Forbidden).

    401 = "who are you?" (no or bad token). 403 = "I know who you are, and no".
    """

    status_code = 403
    code = "forbidden"
    message = "You do not have permission to access this URL."


class InsufficientTierError(ForbiddenError):
    """The user's tier does not include this feature (403)."""

    code = "insufficient_tier"
    message = "This needs a Premium or Admin account."


class FreeTierLimitError(ForbiddenError):
    """A Free account already has the maximum number of active URLs (403)."""

    code = "free_tier_limit"

    def __init__(self, limit: int) -> None:
        """Build the error for the given limit, with the way out in the message."""
        super().__init__(
            message=(
                f"Free accounts can have at most {limit} active URLs. Switch one off or "
                "delete one, or upgrade to Premium for unlimited URLs."
            ),
            details={"limit": limit},
        )


class ShortCodeGenerationError(AppError):
    """Raised when no unique short code could be generated (500 Internal Server Error)."""

    status_code = 500
    code = "short_code_generation_failed"
    message = "Could not generate a unique short code. Please try again."
