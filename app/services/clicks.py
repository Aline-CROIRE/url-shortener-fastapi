"""Click tracking, shared by the API (makes a ClickEvent) and the Celery worker (saves it).

WRITE-BEHIND: the redirect does not save the click itself. It hands a small ticket
(ClickEvent) to a tracker, answers the visitor at once, and a worker saves the
click a moment later. The visitor never waits for the INSERT and the UPDATE.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Protocol

from sqlalchemy import Insert, Update, insert

from app.models import Click
from app.repositories.url import URLRepository

# What a ClickEvent looks like on its way through the queue: only ids and strings
TaskArgs = tuple[int, str | None, str | None, str | None, str | None]


def valid_ip(value: str | None) -> str | None:
    """Return the IP address in its standard text form, or None if it is not one."""
    if not value:
        return None
    try:
        return str(ip_address(value))
    except ValueError:
        return None


def parse_time(value: str | None) -> datetime | None:
    """Read an ISO 8601 time; no time zone means UTC; unreadable means None."""
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


@dataclass(frozen=True)
class ClickEvent:
    """One visit, waiting to be saved. Plain values only: no ORM objects."""

    url_id: int
    ip_address: str | None = None
    user_agent: str | None = None
    referrer: str | None = None
    # When the visitor clicked. A retried task may run minutes later; this keeps
    # the click on the right day in the analytics. None = the database clock.
    clicked_at: datetime | None = None

    def task_args(self) -> TaskArgs:
        """Return the Celery task arguments: JSON-friendly ids and strings."""
        clicked_at = self.clicked_at.isoformat() if self.clicked_at else None
        return (self.url_id, self.ip_address, self.user_agent, self.referrer, clicked_at)

    @classmethod
    def from_task_args(
        cls,
        url_id: int,
        ip_address: str | None,
        user_agent: str | None,
        referrer: str | None,
        clicked_at: str | None,
    ) -> "ClickEvent":
        """Rebuild the event in the worker, checking every value from the queue again."""
        return cls(int(url_id), valid_ip(ip_address), user_agent, referrer, parse_time(clicked_at))


def click_statements(event: ClickEvent) -> tuple[Insert, Update]:
    """Return the two writes of one click; run them in ONE transaction (all or nothing).

    1. INSERT a row into clicks (the visitor's book)
    2. UPDATE urls SET click_count = click_count + 1 (computed by Postgres)
    """
    values: dict[str, object] = {
        "url_id": event.url_id,
        "ip_address": ip_address(event.ip_address) if event.ip_address else None,
        "user_agent": event.user_agent,
        "referrer": event.referrer,
    }
    if event.clicked_at is not None:
        values["clicked_at"] = event.clicked_at  # else the column's default: now()
    return (
        insert(Click).values(**values),
        URLRepository.increment_click_count_statement(event.url_id),
    )


class ClickTracker(Protocol):
    """Something that takes a click and makes sure it gets saved (see app/worker/tracker.py)."""

    async def track(self, event: ClickEvent) -> None:
        """Hand the click over. Must never raise: a lost click must not break a redirect."""
        ...
