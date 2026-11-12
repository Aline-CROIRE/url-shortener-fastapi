from typing import Annotated

from pydantic import StringConstraints

from app.models.tag import TAG_NAME_MAX_LENGTH
from app.schemas.base import ORMModel

# A reusable "tag name" type: spaces around it are removed, then it must be
# 1..50 characters long. "  Marketing " becomes "Marketing"; "   " is refused.
TagName = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=TAG_NAME_MAX_LENGTH),
]


class TagRead(ORMModel):
    id: int
    name: str
