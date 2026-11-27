"""Geo-location of clicks: the worker turns the visitor's IP into a country and a city.

Uses MaxMind's public TEST database (tests/data, made-up sample data). In production
GEOIP_DATABASE_PATH points to a GeoLite2-City file; without one, clicks stay "Unknown".
"""

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Click
from app.worker.geoip import Location, get_geoip_reader, locate
from app.worker.tasks import track_click_task
from tests.test_click_task import seed_url

TEST_DB = Path(__file__).resolve().parent / "data" / "GeoIP2-City-Test.mmdb"
UNKNOWN = Location(None, None)


@pytest.fixture
def reader_from(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    """Open the reader with GEOIP_DATABASE_PATH set to the given path."""

    def open_with(path: str) -> object:
        monkeypatch.setenv("GEOIP_DATABASE_PATH", path)
        get_settings.cache_clear()
        get_geoip_reader.cache_clear()
        return get_geoip_reader()

    yield open_with
    get_geoip_reader.cache_clear()
    get_settings.cache_clear()


@pytest.fixture
def test_reader(reader_from: object, monkeypatch: pytest.MonkeyPatch) -> object:
    reader = reader_from(str(TEST_DB))  # type: ignore[operator]
    assert reader is not None
    monkeypatch.setattr("app.worker.tasks.get_geoip_reader", lambda: reader)
    return reader


# ---------- the lookup ----------


@pytest.mark.parametrize(
    ("ip", "expected"),
    [
        ("81.2.69.142", Location("GB", "London")),
        ("216.160.83.56", Location("US", "Milton")),
        ("89.160.20.112", Location("SE", "Linköping")),  # not only ASCII
        ("2001:218::1", Location("JP", None)),  # IPv6; the country is known, the city not
        ("10.0.0.1", UNKNOWN),  # private network: no location exists
        ("203.0.113.9", UNKNOWN),  # documentation range: not in the database
    ],
)
def test_locate(test_reader: object, ip: str, expected: Location) -> None:
    assert locate(test_reader, ip) == expected  # type: ignore[arg-type]


def test_no_ip_or_no_database_means_unknown(test_reader: object) -> None:
    assert locate(test_reader, None) == UNKNOWN  # type: ignore[arg-type]
    assert locate(None, "81.2.69.142") == UNKNOWN


def test_values_that_do_not_fit_the_columns_are_dropped() -> None:
    class OddReader:
        def get(self, ip: str) -> dict[str, object]:
            return {
                "country": {"iso_code": "GBR"},  # 3 letters: the column holds ISO alpha-2
                "city": {"names": {"en": "x" * 300}},
            }

    location = locate(OddReader(), "81.2.69.142")  # type: ignore[arg-type]

    assert location.country is None
    assert location.city == "x" * 100  # cut to the column size (VARCHAR(100))


def test_a_broken_lookup_never_breaks_the_click(caplog: pytest.LogCaptureFixture) -> None:
    class BrokenReader:
        def get(self, ip: str) -> object:
            raise OSError("the database file was replaced while open")

    with caplog.at_level(logging.WARNING):
        assert locate(BrokenReader(), "81.2.69.142") == UNKNOWN  # type: ignore[arg-type]
    assert "GeoIP lookup failed" in caplog.text


# ---------- opening the database ----------


def test_without_a_path_there_is_no_reader(reader_from: object) -> None:
    assert reader_from("") is None  # type: ignore[operator]


def test_a_missing_file_is_logged_and_clicks_stay_unknown(
    reader_from: object, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        reader = reader_from("/nowhere/GeoLite2-City.mmdb")  # type: ignore[operator]

    assert reader is None
    assert "GeoIP database not usable" in caplog.text


def test_a_file_that_is_not_a_geoip_database_is_refused(
    reader_from: object, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake = tmp_path / "GeoLite2-City.mmdb"
    fake.write_bytes(b"this is not a MaxMind database")

    with caplog.at_level(logging.WARNING):
        assert reader_from(str(fake)) is None  # type: ignore[operator]
    assert "GeoIP database not usable" in caplog.text


def test_the_reader_is_opened_once_per_process(reader_from: object) -> None:
    first = reader_from(str(TEST_DB))  # type: ignore[operator]

    assert get_geoip_reader() is first  # cached: the file is not reopened for every click


# ---------- in the click task ----------


def test_the_task_saves_country_and_city(worker_db: Session, test_reader: object) -> None:
    url = seed_url(worker_db)

    track_click_task.apply(args=(url.id, "81.2.69.142", "Mozilla/5.0")).get()

    worker_db.expire_all()
    click = worker_db.scalar(select(Click).where(Click.url_id == url.id))
    assert click is not None
    assert (click.country, click.city) == ("GB", "London")


def test_without_a_database_the_task_still_saves_the_click(
    worker_db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.worker.tasks.get_geoip_reader", lambda: None)
    url = seed_url(worker_db)

    track_click_task.apply(args=(url.id, "81.2.69.142", "Mozilla/5.0")).get()

    worker_db.expire_all()
    click = worker_db.scalar(select(Click).where(Click.url_id == url.id))
    assert click is not None
    assert (click.country, click.city) == (None, None)  # analytics shows "Unknown"
