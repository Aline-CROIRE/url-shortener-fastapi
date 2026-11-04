from app.storage.base import UrlRecord


class InMemoryUrlRepository:
    """Keeps URLs in a Python dict. Data is lost when the app stops.

    Good for tests and quick local runs. Each worker process has its own dict,
    so use Redis when running several workers.
    """

    def __init__(self) -> None:
        self._store: dict[str, UrlRecord] = {}

    async def save_if_absent(self, record: UrlRecord) -> bool:
        # No "await" between the check and the write, so no other request
        # can run in between: this is atomic inside one event loop.
        if record.short_code in self._store:
            return False
        self._store[record.short_code] = record
        return True

    async def get(self, short_code: str) -> UrlRecord | None:
        return self._store.get(short_code)
