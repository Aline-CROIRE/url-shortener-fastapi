"""GeoIP download script: DB-IP City Lite (free, no account), validated, swapped atomically."""

import gzip
import io
import urllib.error
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from app.scripts import download_geoip as downloader

TEST_DB = Path(__file__).resolve().parent / "data" / "GeoIP2-City-Test.mmdb"


class FakeResponse(io.BytesIO):
    """What urlopen returns: a readable body that works with "with"."""

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def gzipped(data: bytes) -> bytes:
    return gzip.compress(data)


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """URL -> gzipped body; any other URL answers 404, like DB-IP for a missing month."""
    files: dict[str, bytes] = {}

    def fake_urlopen(request: Any, timeout: float = 0) -> FakeResponse:
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if url not in files:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)  # type: ignore[arg-type]
        return FakeResponse(files[url])

    monkeypatch.setattr(downloader.urllib.request, "urlopen", fake_urlopen)
    return files


def test_url_of_a_month() -> None:
    assert downloader.monthly_url(date(2026, 11, 27)) == (
        "https://download.db-ip.com/free/dbip-city-lite-2026-11.mmdb.gz"
    )


def test_previous_month_handles_january() -> None:
    assert downloader.previous_month(date(2027, 1, 3)) == date(2026, 12, 1)


def test_downloads_unpacks_and_validates_the_current_month(
    served: dict[str, bytes], tmp_path: Path
) -> None:
    served[downloader.monthly_url(date(2026, 11, 1))] = gzipped(TEST_DB.read_bytes())

    path = downloader.download(tmp_path, today=date(2026, 11, 27))

    assert path == tmp_path / downloader.FILE_NAME
    assert path.read_bytes() == TEST_DB.read_bytes()  # unpacked (.gz removed)


def test_early_in_the_month_falls_back_to_the_previous_file(
    served: dict[str, bytes], tmp_path: Path
) -> None:
    # On the 1st the new month's file may not be published yet: 404 -> last month's
    served[downloader.monthly_url(date(2026, 10, 1))] = gzipped(TEST_DB.read_bytes())

    path = downloader.download(tmp_path, today=date(2026, 11, 1))

    assert path.read_bytes() == TEST_DB.read_bytes()


def test_a_broken_download_never_replaces_a_working_file(
    served: dict[str, bytes], tmp_path: Path
) -> None:
    working = tmp_path / downloader.FILE_NAME
    working.write_bytes(TEST_DB.read_bytes())
    served[downloader.monthly_url(date(2026, 11, 1))] = gzipped(
        b"an HTML error page, not a database"
    )

    with pytest.raises(SystemExit, match="not a usable City database"):
        downloader.download(tmp_path, today=date(2026, 11, 27))

    assert working.read_bytes() == TEST_DB.read_bytes()  # untouched
    assert sorted(p.name for p in tmp_path.iterdir()) == [downloader.FILE_NAME]  # no leftovers


def test_no_file_for_either_month_is_a_clear_error(
    served: dict[str, bytes], tmp_path: Path
) -> None:
    with pytest.raises(SystemExit, match="No DB-IP file"):
        downloader.download(tmp_path, today=date(2026, 11, 27))


def test_the_request_names_a_generic_client(
    served: dict[str, bytes], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []

    def capture(request: Any, timeout: float = 0) -> FakeResponse:
        seen.append(request)
        return FakeResponse(gzipped(TEST_DB.read_bytes()))

    monkeypatch.setattr(downloader.urllib.request, "urlopen", capture)

    downloader.download(tmp_path, today=date(2026, 11, 27))

    assert seen[0].get_header("User-agent") == downloader.USER_AGENT


def test_main_downloads_into_the_given_folder(
    served: dict[str, bytes], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    served[downloader.monthly_url(date.today())] = gzipped(TEST_DB.read_bytes())

    downloader.main(["--dest", str(tmp_path)])

    assert (tmp_path / downloader.FILE_NAME).exists()
    out = capsys.readouterr().out
    assert "DB-IP" in out  # the attribution the CC BY 4.0 license asks for


def test_a_server_error_stops_with_a_clear_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def server_error(request: Any, timeout: float = 0) -> FakeResponse:
        raise urllib.error.HTTPError(request.full_url, 503, "Unavailable", None, None)  # type: ignore[arg-type]

    monkeypatch.setattr(downloader.urllib.request, "urlopen", server_error)

    # Not a "missing month" (404): no fallback, no half file left behind
    with pytest.raises(SystemExit, match="Download failed"):
        downloader.download(tmp_path, today=date(2026, 11, 27))
    assert list(tmp_path.iterdir()) == []
