from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[3] / ".env", extra="ignore"
    )

    database_url: str = "postgresql+asyncpg://cims_app:change-me@localhost:5432/cims"
    migration_database_url: str = "postgresql+asyncpg://cims_migrator:change-me@localhost:5432/cims"
    jwt_secret: str = Field(min_length=32, default="development-only-change-this-secret-now")
    frontend_origin: str = "http://127.0.0.1:5174"
    cookie_secure: bool = False
    access_token_minutes: int = 15
    session_days: int = 7
    app_env: str = "development"
    refresh_cookie: str = "cims_refresh"
    login_attempt_limit: int = 5
    registration_attempt_limit: int = 5
    catalog_local_mode: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
