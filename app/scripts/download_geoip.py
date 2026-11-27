"""Download the free DB-IP City Lite database, so clicks get a real country and city.

    python -m app.scripts.download_geoip            # into ./geoip/dbip-city-lite.mmdb

DB-IP publishes "IP to City Lite" every month, free and without an account, under the
Creative Commons Attribution 4.0 license (credit: "IP Geolocation by DB-IP",
https://db-ip.com). Same .mmdb format as MaxMind's GeoLite2, so app/worker/geoip.py
reads either. Run it again each month for fresh data, then restart the worker.

Safe to run any time: the new file is checked (it must open as a City database) and
only then swapped in, in one step. A failed or broken download never replaces a
working file.
"""

import argparse
import gzip
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import maxminddb
from maxminddb.errors import InvalidDatabaseError

FILE_NAME = "dbip-city-lite.mmdb"
URL_TEMPLATE = "https://download.db-ip.com/free/dbip-city-lite-{month}.mmdb.gz"
# A generic client name: no personal details in requests to an outside service
USER_AGENT = "url-shortener-geoip-download/1.0"
TIMEOUT_SECONDS = 60
ATTRIBUTION = "IP Geolocation by DB-IP (https://db-ip.com), CC BY 4.0"


def monthly_url(day: date) -> str:
    """Return the download URL of the file published for the month of `day`."""
    return URL_TEMPLATE.format(month=day.strftime("%Y-%m"))


def previous_month(day: date) -> date:
    """Return the first day of the month before `day`'s month."""
    first = day.replace(day=1)
    return (
        first.replace(year=first.year - 1, month=12)
        if first.month == 1
        else first.replace(month=first.month - 1)
    )


def fetch(url: str, target: Path) -> bool:
    """Download `url` (gzipped) and unpack it into `target`; False if there is no such file."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with (
            urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response,
            gzip.GzipFile(fileobj=response) as unpacked,
            target.open("wb") as out,
        ):
            shutil.copyfileobj(unpacked, out)  # streamed: never 60+ MB in memory
    except urllib.error.HTTPError as exc:
        if exc.code == 404:  # not published (yet)
            return False
        raise
    return True


def is_city_database(path: Path) -> bool:
    """Return True if `path` opens as a MaxMind-format City database."""
    try:
        with maxminddb.open_database(str(path)) as reader:
            return "City" in reader.metadata().database_type
    except (InvalidDatabaseError, OSError, ValueError, gzip.BadGzipFile):
        return False


def download(dest: Path, today: date | None = None) -> Path:
    """Download this month's file (or last month's), check it, swap it in; return its path.

    Raises:
        SystemExit: If no file is published, or the download is not a City database.
    """
    today = today or date.today()
    dest.mkdir(parents=True, exist_ok=True)
    final = dest / FILE_NAME
    # A temporary file in the SAME folder: the final rename is then one atomic step
    handle, temp_name = tempfile.mkstemp(dir=dest, prefix=".download-", suffix=".mmdb")
    os.close(handle)
    temp = Path(temp_name)
    try:
        for month in (today, previous_month(today)):  # early in a month: last month's
            try:
                found = fetch(monthly_url(month), temp)
            except (OSError, EOFError, gzip.BadGzipFile) as exc:
                raise SystemExit(f"Download failed: {exc}") from exc
            if found:
                break
        else:
            raise SystemExit(f"No DB-IP file for {today:%Y-%m} or the month before.")
        if not is_city_database(temp):
            raise SystemExit("The download is not a usable City database: nothing replaced.")
        temp.replace(final)  # atomic: readers see the old file or the new one, never half
        return final
    finally:
        temp.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> None:
    """Download into --dest (default ./geoip) and say what to do next."""
    parser = argparse.ArgumentParser(description="Download DB-IP City Lite for click locations.")
    parser.add_argument("--dest", default="geoip", help="folder (default: ./geoip)")
    args = parser.parse_args(argv)
    path = download(Path(args.dest))
    sys.stdout.write(
        f"Saved {path} ({path.stat().st_size / 1_000_000:.0f} MB). {ATTRIBUTION}.\n"
        "Point GEOIP_DATABASE_PATH at this file and restart the worker to use it.\n"
    )


if __name__ == "__main__":
    main()
