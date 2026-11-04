from functools import lru_cache
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config=SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",

    )
    app_name: str = "Url Shortner Dev"
    app_version: str = "0.1.0"
    environment: Literal["development", "production", "testing"] = "development"
    debug: bool = False
    base_url: str = "http://localhost:8000"
    redis_url: str = "redis://localhost:6379/0"
    # "memory" = Python dict (no setup needed), "redis" = Redis server at redis_url
    storage_backend: Literal["memory", "redis"] = "memory"

@lru_cache()
def get_settings() -> Settings:
    return Settings()