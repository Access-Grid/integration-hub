"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path.home() / ".local" / "share"
    return base / "AGSyncTool"


def _default_db_path() -> Path:
    return _data_dir() / "app.db"


def _load_or_create_encryption_key() -> str:
    """Generate and persist a Fernet key on first run if no env var is set.

    Lets a freshly-installed exe start with zero configuration. The key
    is written to %LOCALAPPDATA%\\AGSyncTool\\encryption.key (or the
    XDG-equivalent on POSIX) so secrets at rest survive restarts.
    """
    from cryptography.fernet import Fernet

    key_file = _data_dir() / "encryption.key"
    if key_file.exists():
        existing = key_file.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    key_file.parent.mkdir(parents=True, exist_ok=True)
    new_key = Fernet.generate_key().decode()
    key_file.write_text(new_key, encoding="utf-8")
    try:
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return new_key


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AG_SYNC_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    encryption_key: str = Field(
        default_factory=_load_or_create_encryption_key,
        description="Fernet key used to encrypt secrets at rest. Auto-generated on first run.",
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
