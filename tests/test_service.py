import pytest

from app.core.exceptions import ShortCodeGenerationError, UrlNotFoundError
from app.services import url_shortener
from app.services.url_shortener import UrlShortenerService
from app.storage.memory import InMemoryUrlRepository

pytestmark = pytest.mark.anyio


@pytest.fixture
def service() -> UrlShortenerService:
    return UrlShortenerService(InMemoryUrlRepository(), base_url="http://short.test/")


async def test_create_and_get(service: UrlShortenerService) -> None:
    record = await service.create_short_url("https://example.com")

    assert await service.get_url(record.short_code) == record
    assert service.build_short_url(record.short_code) == f"http://short.test/{record.short_code}"


async def test_get_unknown_raises_not_found(service: UrlShortenerService) -> None:
    with pytest.raises(UrlNotFoundError):
        await service.get_url("missing")


async def test_collision_retries_with_a_new_code(
    service: UrlShortenerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The generator returns "AAAAAAA" twice, then "BBBBBBB"
    codes = iter(["AAAAAAA", "AAAAAAA", "BBBBBBB"])
    monkeypatch.setattr(url_shortener, "generate_code", lambda: next(codes))

    first = await service.create_short_url("https://first.com")
    second = await service.create_short_url("https://second.com")

    assert first.short_code == "AAAAAAA"
    assert second.short_code == "BBBBBBB"  # the clash was detected and retried
    assert (await service.get_url("AAAAAAA")).original_url == "https://first.com"


async def test_gives_up_after_too_many_collisions(
    service: UrlShortenerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(url_shortener, "generate_code", lambda: "AAAAAAA")
    await service.create_short_url("https://first.com")

    with pytest.raises(ShortCodeGenerationError):
        await service.create_short_url("https://second.com")
