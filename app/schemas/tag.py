"""Tag schemas and the reusable TagName type."""

from typing import Annotated

from pydantic import AfterValidator, StringConstraints

from app.models.tag import TAG_NAME_MAX_LENGTH
from app.schemas.base import ORMModel, refuse_invisible_characters

# A reusable "tag name" type: spaces around it are removed, then it must be
# 1..50 characters long, all of them visible. "  Marketing " becomes "Marketing";
# "   ", "a\x00b" or a zero-width space are refused.
TagName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=TAG_NAME_MAX_LENGTH),
    AfterValidator(refuse_invisible_characters),
]


class TagRead(ORMModel):
    """A tag (label) attached to a short URL."""

    id: int
    name: str
