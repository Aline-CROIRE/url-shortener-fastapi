from datetime import datetime

from pydantic import IPvAnyAddress

from app.schemas.base import ORMModel


class ClickRead(ORMModel):
    """One recorded visit of a short link."""

    id: int
    clicked_at: datetime
    ip_address: IPvAnyAddress | None  # IPv4 or IPv6, shown as text in JSON
    user_agent: str | None
    referrer: str | None
    country: str | None
    city: str | None
