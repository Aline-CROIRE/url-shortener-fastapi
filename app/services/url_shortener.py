from datetime import datetime, timezone

from app.core.exceptions import ShortCodeGenerationError, UrlNotFoundError
from app.storage.base import UrlRecord, UrlRepository
from app.utils.codes import generate_code

MAX_CODE_ATTEMPTS = 5


class UrlShortenerService:
    """Business logic for short URLs. It does not know about HTTP or Redis."""

    def __init__(self, repository: UrlRepository, base_url: str) -> None:
        self._repository = repository
        self._base_url = base_url.rstrip("/")

    def build_short_url(self, short_code: str) -> str:
        return f"{self._base_url}/{short_code}"

    async def create_short_url(self, original_url: str) -> UrlRecord:
        # 62^7 (about 3.5 trillion) possible codes, so a clash is very rare;
        # save_if_absent makes sure a clash can never overwrite another URL.
        for _ in range(MAX_CODE_ATTEMPTS):
            record = UrlRecord(
                short_code=generate_code(),
                original_url=original_url,
                created_at=datetime.now(timezone.utc),
            )
            if await self._repository.save_if_absent(record):
                return record
        raise ShortCodeGenerationError()

    async def get_url(self, short_code: str) -> UrlRecord:
        record = await self._repository.get(short_code)
        if record is None:
            raise UrlNotFoundError(short_code)
        return record
