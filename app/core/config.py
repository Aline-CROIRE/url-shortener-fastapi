"""App settings, read from environment variables and the .env file."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 32 random characters: far too many combinations to guess the key offline
MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    """All app settings; each field can be set by an environment variable of the same name."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    app_name: str = "URL Shortener"
    app_version: str = "0.1.0"
    environment: Literal["development", "production", "testing"] = "development"
    debug: bool = False
    base_url: str = "http://localhost:8000"
    # PostgreSQL address: postgresql+asyncpg://<user>:<password>@<host>:<port>/<database>
    database_url: str = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/url_shortener"
    # Optional read-only replica for GET endpoints. Empty = reads use the primary
    database_replica_url: str = ""
    # A separate database used only by pytest, so tests never touch real data
    test_database_url: str = (
        "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/url_shortener_test"
    )
    # True = print every SQL statement in the logs (handy for learning and debugging)
    db_echo: bool = False
    # Redis cache for redirects. Empty = no cache (the app works without Redis)
    redis_url: str = ""
    # How long a redirect stays cached at most (15 minutes)
    cache_ttl_seconds: int = Field(default=900, ge=1)
    # Number of Gunicorn worker processes in production (see gunicorn.conf.py)
    workers: int = Field(default=2, ge=1)

    # ---------- JWT (Module 7) ----------
    # Signs and checks every token. REQUIRED, no default: a default would be written in
    # the code, so everyone could read it and forge tokens. Missing or shorter than 32
    # characters -> Settings() raises and the app refuses to start.
    # SecretStr: printed as '**********'; read it with .get_secret_value()
    jwt_secret_key: SecretStr
    # Token lifetimes (not secret, so they have defaults)
    access_token_minutes: int = Field(default=15, ge=1)
    refresh_token_days: int = Field(default=7, ge=1)

    @field_validator("jwt_secret_key")
    @classmethod
    def check_jwt_secret_key_length(cls, key: SecretStr) -> SecretStr:
        """Refuse a JWT secret shorter than 32 characters (in every environment).

        Raises:
            ValueError: If the key is too short to resist offline guessing.
        """
        if len(key.get_secret_value()) < MIN_SECRET_KEY_LENGTH:
            raise ValueError(
                f"JWT_SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} characters. Create one "
                'with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        return key


@lru_cache
def get_settings() -> Settings:
    """Return the settings, created once and then reused."""
    return Settings()
