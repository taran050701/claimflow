"""Application settings, read from environment variables (or a local .env file).

Keeping config in the environment (not in code) is a 12-factor app rule: the same
code runs on your laptop, in CI, and on Render, with only env vars differing.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://localhost:5432/claimflow"


@lru_cache
def get_settings() -> Settings:
    return Settings()
