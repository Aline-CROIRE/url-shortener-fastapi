from typing import Annotated

from fastapi import Path

from app.utils.codes import CODE_LENGTH

# Shared path parameter: a badly formed code (e.g. "abc" or "bad-code!") gets a 422
# before any storage lookup; a well-formed but unknown code gets a 404.
ShortCodePath = Annotated[
    str,
    Path(
        description=f"{CODE_LENGTH} letters or digits",
        pattern=f"^[A-Za-z0-9]{{{CODE_LENGTH}}}$",
        examples=["aB3dE9x"],
    ),
]
