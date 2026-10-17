"""Application settings, read from environment variables (or a local .env file).

Keeping config in the environment (not in code) is a 12-factor app rule: the same
code runs on your laptop, in CI, and on Render, with only env vars differing.
"""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://localhost:5432/claimflow"
    log_level: str = "INFO"
    # "json" for machines (production, log aggregators), "console" for humans (local dev).
    log_format: str = "json"

    @field_validator("database_url")
    @classmethod
    def use_psycopg_driver(cls, url: str) -> str:
        """Hosts (Neon, Render, Heroku) hand out postgres:// or postgresql:// URLs;
        SQLAlchemy needs the driver named explicitly."""
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url.removeprefix(prefix)
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
