"""App settings, read from environment variables and the .env file."""

from functools import lru_cache
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from limits import parse as parse_rate_limit
from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url

# 32 random characters: far too many combinations to guess the key offline
MIN_SECRET_KEY_LENGTH = 32
# Redis address schemes (rediss = Redis over TLS)
REDIS_SCHEMES = ("redis://", "rediss://")
# Storages the rate limits can use
RATE_LIMIT_STORAGE_SCHEMES = ("memory://", *REDIS_SCHEMES)


def to_sync_url(database_url: str) -> str:
    """Return the same database address with the synchronous psycopg driver."""
    url = make_url(database_url).set(drivername="postgresql+psycopg")
    return url.render_as_string(hide_password=False)


def redis_target(url: str) -> tuple[str, int, str]:
    """Return (host, port, DB number) of a Redis URL; no number means DB "0"."""
    parts = urlsplit(url)
    return (parts.hostname or "", parts.port or 6379, parts.path.strip("/") or "0")


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
    # ---------- OpenTelemetry tracing (standard OTEL_* names) ----------
    # Where traces go, over OTLP/HTTP, e.g. http://jaeger:4318. Empty = tracing OFF
    otel_exporter_otlp_endpoint: str = ""
    # Shown in the tracing UI as "<name>-api" and "<name>-worker"
    otel_service_name: str = "url-shortener"
    # Share of new traces kept: 1.0 = all (development), e.g. 0.1 = 10% under heavy load
    otel_traces_sampler_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    # Protects GET /metrics: Prometheus must send "Authorization: Bearer <token>".
    # Not set = open (fine when /metrics is only reachable on an internal network)
    metrics_token: SecretStr | None = None
    # "json": one JSON object per line (production, log collectors); "text": for reading by eye
    log_format: Literal["json", "text"] = "json"
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
    # Celery's job queue (the "broker"). Another Redis DB number than the cache
    # (/1, cache /0): clearing the cache can never delete queued jobs.
    celery_broker_url: str = "redis://127.0.0.1:6379/1"
    # MaxMind GeoLite2-City file for click locations (used by the Celery worker).
    # Empty = no lookup: clicks are saved with an "Unknown" country and city.
    geoip_database_path: str = ""
    # ---------- production sizing (see app/core/capacity.py for the math) ----------
    # Gunicorn worker processes. Empty = 2 x CPU cores + 1. The standard name
    # (Uvicorn and most hosting platforms read WEB_CONCURRENCY too).
    web_concurrency: int | None = Field(default=None, ge=1)
    # Database connections kept open per web worker, plus extra ones for busy moments
    db_pool_size: int = Field(default=3, ge=1)
    db_max_overflow: int = Field(default=2, ge=0)
    # Seconds a request waits for a free pooled connection before it fails
    db_pool_timeout: float = Field(default=10, gt=0)
    # PostgreSQL's max_connections (SHOW max_connections); Gunicorn refuses to start
    # if the workers could need more
    db_max_connections: int = Field(default=100, ge=20)
    # AnyIO's thread pool per web worker: blocking calls run here (track_click_task.delay,
    # reading the metrics files). Threads mostly WAIT (network, disk), so 40 is cheap.
    # Password hashing has its own small pool (app/services/password_work.py).
    thread_pool_size: int = Field(default=40, ge=1)
    # Celery worker processes (the --concurrency of the celery-worker container)
    celery_concurrency: int = Field(default=2, ge=1)

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
    # Short URLs one USER may create: stops mass creation of spam or phishing links
    create_url_rate_limit: str = "30/minute"
    # FAILED logins allowed per account (email), from any address: stops a botnet
    login_account_rate_limit: str = "10/hour"
    # Where the rate-limit counters live. Empty = REDIS_URL when set (shared by every
    # worker, kept across restarts), else memory:// (per worker). "redis://..." or
    # "rediss://..." (TLS) or "memory://".
    rate_limit_storage_uri: str = ""

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

    @field_validator(
        "login_rate_limit",
        "register_rate_limit",
        "create_url_rate_limit",
        "login_account_rate_limit",
    )
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

    @field_validator("rate_limit_storage_uri")
    @classmethod
    def check_rate_limit_storage(cls, value: str) -> str:
        """Refuse a storage the rate limits cannot use (a typo must stop the app).

        Raises:
            ValueError: If it is not empty, memory://, redis://... or rediss://...
        """
        if value and not value.startswith(RATE_LIMIT_STORAGE_SCHEMES):
            raise ValueError(
                "RATE_LIMIT_STORAGE_URI must be empty, memory://, redis://... or "
                f"rediss://..., not {value!r}"
            )
        return value

    @field_validator("metrics_token", mode="before")
    @classmethod
    def empty_token_means_none(cls, value: Any) -> Any:
        """Read "METRICS_TOKEN=" (empty) as "no token", not as an empty secret."""
        return None if value == "" else value

    @field_validator("celery_broker_url")
    @classmethod
    def check_broker_url(cls, value: str) -> str:
        """Refuse a broker that is not Redis (a typo must stop the app and the worker).

        Raises:
            ValueError: If it does not start with redis:// or rediss://.
        """
        if not value.startswith(REDIS_SCHEMES):
            raise ValueError(f"CELERY_BROKER_URL must start with redis:// or rediss://: {value!r}")
        return value

    @model_validator(mode="after")
    def check_cache_and_broker_use_different_dbs(self) -> "Settings":
        """Refuse a broker in the same Redis DB as the cache.

        Raises:
            ValueError: If both point to the same server and DB number.
        """
        if self.redis_url and redis_target(self.redis_url) == redis_target(self.celery_broker_url):
            raise ValueError(
                "REDIS_URL and CELERY_BROKER_URL must use a different Redis DB "
                "(e.g. .../0 for the cache, .../1 for the broker)"
            )
        return self

    @property
    def sync_database_url(self) -> str:
        """Return DATABASE_URL with the synchronous psycopg driver (for the Celery worker)."""
        return to_sync_url(self.database_url)

    @property
    def rate_limit_storage(self) -> str:
        """Return where the rate-limit counters live (see rate_limit_storage_uri)."""
        return self.rate_limit_storage_uri or self.redis_url or "memory://"

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
