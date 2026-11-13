"""App settings, read from environment variables and the .env file."""

from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Only for development and tests. Production refuses to start with it (see below).
DEV_SECRET_KEY = "dev-only-secret-key-change-me-in-production"
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
    # Signs the login cookie so nobody can change it. A long random secret in production
    secret_key: str = DEV_SECRET_KEY
    # How long a login lasts (1 day)
    session_max_age_seconds: int = Field(default=86_400, ge=60)
    # True = the login cookie is only sent over HTTPS (turn on behind HTTPS in production)
    session_https_only: bool = False

    @model_validator(mode="after")
    def check_production_secret_key(self) -> Self:
        """Refuse to start in production with the development or a too-short secret key.

        Raises:
            ValueError: If ENVIRONMENT is production and SECRET_KEY is not a real secret.
        """
        if self.environment == "production" and (
            self.secret_key == DEV_SECRET_KEY or len(self.secret_key) < MIN_SECRET_KEY_LENGTH
        ):
            raise ValueError(
                f"Set SECRET_KEY to a random string of at least {MIN_SECRET_KEY_LENGTH} "
                "characters in production"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the settings, created once and then reused."""
    return Settings()
