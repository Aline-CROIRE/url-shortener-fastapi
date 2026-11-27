"""Module 8 infrastructure: broker settings, the Celery app and the Docker Compose files.

The compose files are read as YAML, so a missing service, healthcheck or start
order fails here, not only when someone runs "docker compose up".
"""

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from app.core.config import Settings
from app.worker.celery_app import create_celery_app

ROOT = Path(__file__).resolve().parent.parent
GOOD_KEY = "k" * 32
CELERY_SERVICES = ("celery-worker", "celery-beat")


def make_settings(monkeypatch: pytest.MonkeyPatch, **values: object) -> Settings:
    """Settings from ONLY the given values: no environment variable, no .env file."""
    for name in ("JWT_SECRET_KEY", "REDIS_URL", "CELERY_BROKER_URL", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None, jwt_secret_key=GOOD_KEY, **values)  # type: ignore[call-arg]


def load_compose(name: str) -> dict[str, Any]:
    return yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))


def redis_db(url: str) -> str:
    """The Redis DB number in a URL ("redis://host:6379/1" -> "1"; no number = "0")."""
    return url.rsplit("/", 1)[-1] if url.count("/") > 2 else "0"


# ---------- settings ----------


def test_broker_defaults_to_redis_db_1(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(monkeypatch)

    assert settings.celery_broker_url == "redis://127.0.0.1:6379/1"


@pytest.mark.parametrize(
    ("cache", "broker"),
    [
        ("redis://127.0.0.1:6379/0", "redis://127.0.0.1:6379/0"),
        ("redis://127.0.0.1:6379", "redis://127.0.0.1:6379/0"),  # no number = DB 0
        ("redis://redis:6379/2", "redis://redis:6379/2/"),
    ],
)
def test_broker_on_the_cache_db_is_refused(
    monkeypatch: pytest.MonkeyPatch, cache: str, broker: str
) -> None:
    # Same DB: a FLUSHDB to clear the cache would also delete every queued job
    with pytest.raises(ValidationError, match="different Redis DB"):
        make_settings(monkeypatch, redis_url=cache, celery_broker_url=broker)


def test_broker_on_another_redis_server_may_use_the_same_db_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(
        monkeypatch, redis_url="redis://cache:6379/0", celery_broker_url="redis://queue:6379/0"
    )

    assert settings.celery_broker_url == "redis://queue:6379/0"


@pytest.mark.parametrize("broker", ["", "amqp://guest@rabbit//", "memory://", "6379/1"])
def test_broker_must_be_a_redis_url(monkeypatch: pytest.MonkeyPatch, broker: str) -> None:
    with pytest.raises(ValidationError, match="CELERY_BROKER_URL"):
        make_settings(monkeypatch, celery_broker_url=broker)


def test_sync_database_url_swaps_only_the_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(
        monkeypatch, database_url="postgresql+asyncpg://user:p%40ss@db:5432/urls"
    )

    # The worker is synchronous: same database, same password, psycopg driver
    assert settings.sync_database_url == "postgresql+psycopg://user:p%40ss@db:5432/urls"


# ---------- the Celery app ----------


def test_celery_app_uses_the_given_broker() -> None:
    celery = create_celery_app("redis://queue:6379/1")

    assert celery.conf.broker_url == "redis://queue:6379/1"


def test_celery_app_accepts_only_json_and_stores_no_results() -> None:
    celery = create_celery_app("redis://queue:6379/1")

    # JSON only: a pickled message from the queue could run code in the worker
    assert celery.conf.task_serializer == "json"
    assert celery.conf.accept_content == ["json"]
    # Nobody reads task results, so none are written to Redis
    assert celery.conf.task_ignore_result is True
    assert celery.conf.timezone == "UTC"
    assert celery.conf.enable_utc is True


# ---------- .env.example ----------


def test_env_example_puts_cache_and_broker_in_different_redis_dbs() -> None:
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    values = dict(line.split("=", 1) for line in lines if "=" in line and not line.startswith("#"))

    assert redis_db(values["REDIS_URL"]) == "0"
    assert redis_db(values["CELERY_BROKER_URL"]) == "1"


# ---------- docker compose ----------


@pytest.mark.parametrize("compose_file", ["docker-compose.yml", "docker-compose.prod.yml"])
@pytest.mark.parametrize("service", CELERY_SERVICES)
def test_celery_service_has_healthcheck_and_waits_for_healthy_redis(
    compose_file: str, service: str
) -> None:
    services = load_compose(compose_file)["services"]

    assert service in services, f"{service} missing in {compose_file}"
    config = services[service]
    assert config["build"] == ".", "same image as the app: the tasks import app code"
    assert "test" in config["healthcheck"]
    assert config["depends_on"]["redis"]["condition"] == "service_healthy"


@pytest.mark.parametrize("compose_file", ["docker-compose.yml", "docker-compose.prod.yml"])
def test_worker_starts_only_after_migrations(compose_file: str) -> None:
    worker = load_compose(compose_file)["services"]["celery-worker"]

    # The worker writes clicks: the tables must exist first
    assert worker["depends_on"]["migrate"]["condition"] == "service_completed_successfully"


@pytest.mark.parametrize("compose_file", ["docker-compose.yml", "docker-compose.prod.yml"])
def test_beat_writes_its_files_to_tmp(compose_file: str) -> None:
    beat = load_compose(compose_file)["services"]["celery-beat"]

    # The image runs as a non-root user who cannot write into /app
    command = beat["command"]
    assert "--schedule=/tmp/" in command
    assert "--pidfile=/tmp/" in command


@pytest.mark.parametrize("compose_file", ["docker-compose.yml", "docker-compose.prod.yml"])
@pytest.mark.parametrize("service", ["app", *CELERY_SERVICES])
def test_compose_puts_cache_and_broker_in_different_redis_dbs(
    compose_file: str, service: str
) -> None:
    env = load_compose(compose_file)["services"][service]["environment"]

    assert redis_db(env["REDIS_URL"]) == "0"
    assert redis_db(env["CELERY_BROKER_URL"]) == "1"


def test_production_redis_never_evicts_queued_jobs() -> None:
    command = load_compose("docker-compose.prod.yml")["services"]["redis"]["command"]

    # The eviction policy is per SERVER, not per DB. allkeys-lru could delete a
    # queued job; volatile-lru only removes keys with a TTL (cache entries).
    assert "volatile-lru" in command
    assert "allkeys-lru" not in command
