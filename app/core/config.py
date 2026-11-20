"""App settings, read from environment variables and the .env file."""

from functools import lru_cache
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from limits import parse as parse_rate_limit
from pydantic import Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# 32 random characters: far too many combinations to guess the key offline
MIN_SECRET_KEY_LENGTH = 32


def normalize_host(host: str) -> str:
    """Return a host name in the one form used for comparing hosts.

    Lowercase, no trailing dot ("Short.Test." -> "short.test"), and international
    names in their ASCII "xn--" form, which is how HttpUrl stores them.
    """
    host = host.strip().lower().rstrip(".")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


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
    # Other host names this shortener answers on (BASE_URL's host is always included).
    # Short links may never point to them: the link would redirect to itself. A
    # comma-separated list in the environment: OWN_HOSTS=sho.rt,www.sho.rt
    # NoDecode: read the text as it is (not as JSON); split_own_hosts splits it.
    own_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)
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
    # Login attempts allowed per client ("5/minute", "20/hour", ...)
    login_rate_limit: str = "5/minute"
    # New accounts allowed per client: each costs a slow hash and could be spam
    register_rate_limit: str = "10/hour"

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

    @field_validator("own_hosts", mode="before")
    @classmethod
    def split_own_hosts(cls, value: Any) -> Any:
        """Accept "a.com, b.com" (the environment) as well as a list; normalise each host."""
        hosts = value.split(",") if isinstance(value, str) else value
        if not isinstance(hosts, list):
            return value  # pydantic reports the wrong type
        return [normalize_host(str(host)) for host in hosts if str(host).strip(" .")]

    @field_validator("login_rate_limit", "register_rate_limit")
    @classmethod
    def check_rate_limit(cls, value: str, info: ValidationInfo) -> str:
        """Refuse a rate limit that cannot be read, or one that allows no login at all.

        A typo must stop the app at startup, not switch the protection off silently.

        Raises:
            ValueError: If the value is not like "5/minute" with at least 1 attempt.
        """
        try:
            limit = parse_rate_limit(value)
        except ValueError:
            limit = None
        if limit is None or limit.amount < 1:
            name = (info.field_name or "rate_limit").upper()
            raise ValueError(f'{name} must look like "5/minute" (at least 1), not {value!r}')
        return value

    @property
    def own_host_names(self) -> frozenset[str]:
        """Every host name of this shortener: BASE_URL's host plus OWN_HOSTS."""
        base_host = urlsplit(self.base_url).hostname
        names = {normalize_host(base_host)} if base_host else set()
        return frozenset(names | set(self.own_hosts))


@lru_cache
def get_settings() -> Settings:
    """Return the settings, created once and then reused."""
    return Settings()
