"""Cursor ("bookmark") text for the newest-first URL list."""

import base64
import json
from datetime import UTC, datetime

import pytest

from app.core.exceptions import InvalidCursorError
from app.services.pagination import decode_cursor, encode_cursor

MOMENT = datetime(2026, 11, 12, 10, 0, 0, 123456, tzinfo=UTC)


def raw_cursor(payload: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def test_round_trip() -> None:
    cursor = encode_cursor((MOMENT, 42))

    assert decode_cursor(cursor) == (MOMENT, 42)  # microseconds kept
    assert "=" not in cursor  # no padding: safe in a query string


@pytest.mark.parametrize(
    "cursor",
    [
        "not base64 at all!!",
        base64.urlsafe_b64encode(b"not json").decode(),
        raw_cursor({"values": ["2026-11-12T10:00:00+00:00", 1]}),  # no sort
        raw_cursor({"sort": "newest", "values": ["yesterday", 1]}),
        raw_cursor({"sort": "newest", "values": ["2026-11-12T10:00:00", 1]}),  # no timezone
        raw_cursor({"sort": "newest", "values": ["2026-11-12T10:00:00+00:00", "1"]}),
        raw_cursor({"sort": "newest", "values": ["2026-11-12T10:00:00+00:00", True]}),
        raw_cursor({"sort": "newest", "values": ["2026-11-12T10:00:00+00:00"]}),
        # an old most_clicked bookmark: click counts change, so they have no cursors
        raw_cursor({"sort": "most_clicked", "values": [3, 1]}),
    ],
)
def test_broken_or_forged_cursors_are_rejected(cursor: str) -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor(cursor)
