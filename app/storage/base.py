from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class UrlRecord:
    """One saved short URL, independent of how it is stored."""

    short_code: str
    original_url: str
    created_at: datetime


class UrlRepository(Protocol):
    """The contract every storage backend must follow.

    Any class with these methods counts as a UrlRepository; it does not need
    to inherit from this class (that is how typing.Protocol works).
    """

    async def save_if_absent(self, record: UrlRecord) -> bool:
        """Save the record only if its short code is free.

        Returns True if it was saved, False if the code already exists.
        """
        ...

    async def get(self, short_code: str) -> UrlRecord | None:
        """Return the record for this short code, or None if it does not exist."""
        ...
