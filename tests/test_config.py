"""The JWT settings: a strong secret is required, lifetimes have safe defaults."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings

GOOD_KEY = "k" * 32  # exactly the minimum length


def make_settings(monkeypatch: pytest.MonkeyPatch, **values: object) -> Settings:
    """Build Settings from ONLY the given values: no environment variable, no .env file."""
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_missing_jwt_secret_key_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="jwt_secret_key"):
        make_settings(monkeypatch)


def test_short_jwt_secret_key_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET_KEY must be at least 32 characters"):
        make_settings(monkeypatch, jwt_secret_key="k" * 31)


def test_32_character_key_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(monkeypatch, jwt_secret_key=GOOD_KEY)

    assert settings.jwt_secret_key.get_secret_value() == GOOD_KEY


def test_secret_key_is_hidden_when_printed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(monkeypatch, jwt_secret_key=GOOD_KEY)

    assert GOOD_KEY not in repr(settings)
    assert GOOD_KEY not in str(settings.jwt_secret_key)


def test_token_lifetimes_default_to_15_minutes_and_7_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ACCESS_TOKEN_MINUTES", raising=False)
    monkeypatch.delenv("REFRESH_TOKEN_DAYS", raising=False)
    settings = make_settings(monkeypatch, jwt_secret_key=GOOD_KEY)

    assert settings.access_token_minutes == 15
    assert settings.refresh_token_days == 7


@pytest.mark.parametrize("field", ["access_token_minutes", "refresh_token_days"])
def test_token_lifetime_must_be_positive(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        make_settings(monkeypatch, jwt_secret_key=GOOD_KEY, **{field: 0})
