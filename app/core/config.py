from functools import lru_cache
from typing import Literal
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config=SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",

    )
    app_name: str = "URL Shortener"
    app_version: str = "0.1.0"
    environment: Literal["development", "production", "testing"] = "development"
    debug: bool = False
    base_url: str = "http://localhost:8000"
    redis_url: str = "redis://127.0.0.1:6379/0"
    # "memory" = Python dict (no setup needed), "redis" = Redis server at redis_url
    storage_backend: Literal["memory", "redis"] = "memory"
    # PostgreSQL address: postgresql+asyncpg://<user>:<password>@<host>:<port>/<database>
    database_url: str = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/url_shortener"
    # A separate database used only by pytest, so tests never touch real data
    test_database_url: str = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/url_shortener_test"
    # True = print every SQL statement in the logs (handy for learning and debugging)
    db_echo: bool = False
    # Number of Gunicorn worker processes in production (see gunicorn.conf.py)
    workers: int = Field(default=2, ge=1)

@lru_cache()
def get_settings() -> Settings:
    return Settings()