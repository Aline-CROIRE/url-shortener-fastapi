"""Geo-location of clicks: visitor IP -> country and city, looked up by the Celery worker.

Done in the worker (write-behind), never in the redirect: the visitor does not wait.
The data comes from a MaxMind ".mmdb" file (GeoLite2-City, free with a MaxMind
account), set with GEOIP_DATABASE_PATH. Without a file every click stays "Unknown",
exactly as before: geo-location is an extra, never a reason to lose a click.
"""

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

import maxminddb
from maxminddb.errors import InvalidDatabaseError

from app.core.config import get_settings

logger = logging.getLogger(__name__)

COUNTRY_CODE_LENGTH = 2  # ISO 3166-1 alpha-2, e.g. "RW" (the clicks.country column)
CITY_MAX_LENGTH = 100  # the clicks.city column


class GeoReader(Protocol):
    """What we use of maxminddb.Reader: one lookup by IP address."""

    def get(self, ip_address: str) -> Any:
        """Return the record for this IP, or None if it is not in the database."""
        ...


@dataclass(frozen=True)
class Location:
    """Where a visitor came from; None = unknown."""

    country: str | None
    city: str | None


UNKNOWN = Location(None, None)


@lru_cache
def get_geoip_reader() -> GeoReader | None:
    """Open the GeoIP database once per worker process; None if not configured or unusable.

    The file is memory-mapped: lookups read it without loading it all into memory.
    """
    path = get_settings().geoip_database_path
    if not path:
        return None
    try:
        reader: GeoReader = maxminddb.open_database(path)
    except (OSError, InvalidDatabaseError, ValueError):
        logger.warning(
            "GeoIP database not usable (%s): clicks are saved without a location",
            path,
            exc_info=True,
        )
        return None
    return reader


def locate(reader: GeoReader | None, ip_address: str | None) -> Location:
    """Return the country (ISO code) and city (English name) of an IP, or UNKNOWN.

    Never raises: a failed lookup must not cost us the click itself.
    """
    if reader is None or not ip_address:
        return UNKNOWN
    try:
        record = reader.get(ip_address) or {}
    except (OSError, ValueError, InvalidDatabaseError):
        logger.warning("GeoIP lookup failed for one click", exc_info=True)
        return UNKNOWN
    country = (record.get("country") or {}).get("iso_code")
    city = ((record.get("city") or {}).get("names") or {}).get("en")
    return Location(
        # Only a real 2-letter code fits the column (VARCHAR(2)); anything else = unknown
        country=country
        if isinstance(country, str) and len(country) == COUNTRY_CODE_LENGTH
        else None,
        city=city[:CITY_MAX_LENGTH] if isinstance(city, str) and city else None,
    )
