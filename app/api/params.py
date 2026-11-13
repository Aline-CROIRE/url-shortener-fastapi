"""Shared path parameters for the API routes."""

from typing import Annotated

from fastapi import Path

from app.models.url import SHORT_CODE_MAX_LENGTH

SHORT_CODE_MIN_LENGTH = 3

# Shared path parameter: random codes (7 letters/digits) and custom aliases
# (3-10 letters, digits, "-" or "_"). A badly formed code gets a 422 before any
# database lookup; a well-formed but unknown code gets a 404.
ShortCodePath = Annotated[
    str,
    Path(
        description=(
            f"{SHORT_CODE_MIN_LENGTH}-{SHORT_CODE_MAX_LENGTH} letters, digits, '-' or '_'"
        ),
        pattern=f"^[A-Za-z0-9_-]{{{SHORT_CODE_MIN_LENGTH},{SHORT_CODE_MAX_LENGTH}}}$",
        examples=["aB3dE9x"],
    ),
]
