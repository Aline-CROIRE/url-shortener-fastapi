"""The shared base class for output schemas, and text rules shared by inputs."""

from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict


class ORMModel(BaseModel):
    """Base class for every OUTPUT schema.

    from_attributes=True lets Pydantic read values from object attributes
    (url.short_code) instead of dict keys (url["short_code"]). That works for
    SQLAlchemy models and for SQL result rows with .label() names.
    """

    model_config = ConfigDict(from_attributes=True)


# ---------- text rules shared by input schemas and query parameters ----------


def refuse_nul(text: str | None) -> str | None:
    r"""Refuse a NUL character (\x00) in text that goes to the database.

    PostgreSQL cannot store \x00 in a text column: without this check the request
    fails with a 500 instead of a clear 422.

    Raises:
        ValueError: If the text contains \x00.
    """
    if text is not None and "\x00" in text:
        raise ValueError(r"must not contain a NUL character (\x00)")
    return text


def refuse_invisible_characters(text: str | None) -> str | None:
    r"""Refuse control and invisible characters (\x00, line breaks, zero-width ...).

    For short single-line text shown to people (tag names, titles): what is
    stored must be what is seen.

    Raises:
        ValueError: If the text has a character that is not printable.
    """
    if text is not None and not text.isprintable():
        raise ValueError("must not contain control or invisible characters")
    return text


# Text that is safe to store; single-line text that is also safe to show
SafeText = Annotated[str, AfterValidator(refuse_nul)]
VisibleText = Annotated[str, AfterValidator(refuse_invisible_characters)]
