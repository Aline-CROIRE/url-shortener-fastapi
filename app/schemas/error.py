"""The JSON shape of every error response."""

from typing import Any

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    """What went wrong: an error code, a readable message and optional extra details."""

    code: str = Field(description="Short machine-readable error code", examples=["url_not_found"])
    message: str = Field(
        description="Human-readable explanation",
        examples=["Short URL 'aB3dE9x' was not found."],
    )
    details: Any = Field(default=None, description="Extra information, or null")


class ErrorResponse(BaseModel):
    """The one JSON shape used for every error response."""

    error: ErrorDetail
