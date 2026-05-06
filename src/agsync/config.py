"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_db_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path.home() / ".local" / "share"
    return base / "AGSyncTool" / "app.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AG_SYNC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    encryption_key: str = Field(
        ...,
        description="Fernet key used to encrypt secrets at rest. Required.",
    )
    host: str = "0.0.0.0"
    port: int = 5355
    db_path: Path = Field(default_factory=_default_db_path)
    sentry_dsn: str = Field(
        default="",
        description="Sentry DSN. Baked at build time; empty disables Sentry.",
    )
    log_level: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    return settings
