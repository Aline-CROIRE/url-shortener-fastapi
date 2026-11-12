import base64
import json
from datetime import UTC, datetime

import pytest

from app.core.exceptions import InvalidCursorError
from app.repositories.url import URLSort
from app.services.pagination import decode_cursor, encode_cursor

MOMENT = datetime(2026, 11, 12, 10, 0, 0, 123456, tzinfo=UTC)


def raw_cursor(payload: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


def test_round_trip_newest() -> None:
    cursor = encode_cursor(URLSort.NEWEST, (MOMENT, 42))

    assert decode_cursor(cursor, URLSort.NEWEST) == (MOMENT, 42)  # microseconds kept
    assert "=" not in cursor  # no padding: safe in a query string


def test_round_trip_most_clicked() -> None:
    cursor = encode_cursor(URLSort.MOST_CLICKED, (7, 42))
    assert decode_cursor(cursor, URLSort.MOST_CLICKED) == (7, 42)


def test_cursor_from_another_sort_is_rejected() -> None:
    cursor = encode_cursor(URLSort.NEWEST, (MOMENT, 42))
    with pytest.raises(InvalidCursorError, match="different sort"):
        decode_cursor(cursor, URLSort.MOST_CLICKED)


@pytest.mark.parametrize(
    ("cursor", "sort"),
    [
        ("not base64 at all!!", URLSort.NEWEST),
        (base64.urlsafe_b64encode(b"not json").decode(), URLSort.NEWEST),
        (raw_cursor({"values": ["2026-11-12T10:00:00+00:00", 1]}), URLSort.NEWEST),  # no sort
        (raw_cursor({"sort": "newest", "values": ["yesterday", 1]}), URLSort.NEWEST),
        (raw_cursor({"sort": "newest", "values": ["2026-11-12T10:00:00", 1]}), URLSort.NEWEST),  # no tz
        (raw_cursor({"sort": "newest", "values": ["2026-11-12T10:00:00+00:00", "1"]}), URLSort.NEWEST),
        (raw_cursor({"sort": "newest", "values": ["2026-11-12T10:00:00+00:00", True]}), URLSort.NEWEST),
        (raw_cursor({"sort": "most_clicked", "values": [-1, 1]}), URLSort.MOST_CLICKED),
        (raw_cursor({"sort": "most_clicked", "values": [1]}), URLSort.MOST_CLICKED),
    ],
)
def test_broken_or_forged_cursors_are_rejected(cursor: str, sort: URLSort) -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor(cursor, sort)
