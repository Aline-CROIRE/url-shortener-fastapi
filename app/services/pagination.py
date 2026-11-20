"""Keyset pagination cursors ("bookmarks") for the newest-first URL list.

A cursor stores the last row's position, e.g.
{"sort": "newest", "values": ["2026-11-12T10:00:00+00:00", 42]},
as URL-safe base64 text so it can travel in a query string.
Only created_at and id are used: they never change, so a bookmark stays correct
(click_count changes on every visit, so "most_clicked" has no cursors).
It is not secret or signed: it only points into the caller's OWN URLs (the
owner filter is always applied), so tampering cannot reveal anyone else's data.
"""

import base64
import binascii
import json
from datetime import UTC, datetime
from typing import Any

from app.core.exceptions import InvalidCursorError
from app.repositories.url import CursorPosition, URLSort

# The largest value of a Postgres INTEGER column (urls.id)
MAX_ROW_ID = 2**31 - 1


def encode_cursor(position: CursorPosition) -> str:
    """Turn the last row's (created_at, id) into URL-safe cursor text."""
    created_at, row_id = position
    payload = {"sort": URLSort.NEWEST.value, "values": [created_at.isoformat(), row_id]}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")  # "=" padding is noise in URLs


def decode_cursor(cursor: str) -> CursorPosition:
    """Turn the cursor text back into (created_at, id).

    Raises:
        InvalidCursorError: If the cursor is broken or forged (400).
    """
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload: Any = json.loads(base64.urlsafe_b64decode(padded))
        if payload["sort"] != URLSort.NEWEST.value:
            raise ValueError("only newest-first lists have cursors")
        value, row_id = payload["values"]
        if not isinstance(row_id, int) or isinstance(row_id, bool):
            raise ValueError("id must be an integer")
        # urls.id is a Postgres INTEGER: anything outside 1..2^31-1 is forged, and
        # would make the database fail (a 500) instead of a clean 400
        if not 1 <= row_id <= MAX_ROW_ID:
            raise ValueError("id is out of range")
        created_at = datetime.fromisoformat(value)
        if created_at.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        # In UTC it must still be a date Python and Postgres can hold (e.g. not the
        # year 10000 that "9999-12-31T23:59:59-23:59" becomes)
        created_at.astimezone(UTC)
        return created_at, row_id
    except (
        binascii.Error,
        UnicodeDecodeError,
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
    ) as exc:
        raise InvalidCursorError() from exc
