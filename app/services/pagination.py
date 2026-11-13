"""Keyset pagination cursors ("bookmarks").

A cursor stores the sort and the last row's position, e.g.
{"sort": "newest", "values": ["2026-11-12T10:00:00+00:00", 42]},
as URL-safe base64 text so it can travel in a query string.
It is not secret or signed: it only points into the caller's OWN URLs (the
owner filter is always applied), so tampering cannot reveal anyone else's data.
"""

import base64
import binascii
import json
from datetime import datetime
from typing import Any

from app.core.exceptions import InvalidCursorError
from app.repositories.url import CursorPosition, URLSort


def encode_cursor(sort: URLSort, position: CursorPosition) -> str:
    """Turn the sort and the last row's (sort value, id) into URL-safe cursor text."""
    value, row_id = position
    payload = {
        "sort": sort.value,
        "values": [value.isoformat() if isinstance(value, datetime) else value, row_id],
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")  # "=" padding is noise in URLs


def decode_cursor(cursor: str, sort: URLSort) -> CursorPosition:
    """Turn the cursor text back into (sort value, id).

    Raises:
        InvalidCursorError: If the cursor is broken or belongs to another sort (400).
    """
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload: Any = json.loads(base64.urlsafe_b64decode(padded))
        if payload["sort"] != sort.value:
            raise InvalidCursorError("This cursor belongs to a different sort order.")
        value, row_id = payload["values"]
        if not isinstance(row_id, int) or isinstance(row_id, bool):
            raise ValueError("id must be an integer")
        if sort is URLSort.NEWEST:
            created_at = datetime.fromisoformat(value)
            if created_at.tzinfo is None:
                raise ValueError("created_at must include a timezone")
            return created_at, row_id
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("click_count must be a non-negative integer")
        return value, row_id
    except InvalidCursorError:
        raise
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
        raise InvalidCursorError() from exc
