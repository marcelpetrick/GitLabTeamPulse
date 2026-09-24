"""Application configuration from environment variables, an optional .env file and secrets.

Every setting uses the ``TEAMPULSE_`` prefix, e.g. ``TEAMPULSE_GITLAB_URL``.
The GitLab token is resolved from a secret file first (Docker secrets), then from the
environment. It is held as a ``SecretStr`` so it never leaks through ``repr`` or logs.
"""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SECRET_FILE = Path("/run/secrets/gitlab_token")


def default_database_path() -> Path:
    """Return the default SQLite location, outside the source tree."""
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data_home) / "gitlab-team-pulse" / "teampulse.db"


class Settings(BaseSettings):
    """Runtime configuration. No setting requires a source-code change."""

    model_config = SettingsConfigDict(
        env_prefix="TEAMPULSE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gitlab_url: str = ""
    gitlab_token: SecretStr | None = None
    gitlab_token_file: Path | None = None
    gitlab_verify_tls: bool = True
    gitlab_ca_bundle: Path | None = None
    gitlab_timeout_seconds: float = Field(default=20.0, gt=0)
    gitlab_max_retries: int = Field(default=3, ge=0, le=10)
    gitlab_concurrency: int = Field(default=4, ge=1, le=32)

    database_path: Path = Field(default_factory=default_database_path)

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: str = "INFO"

    user_refresh_interval_seconds: int = Field(default=3600, ge=60)
    selected_refresh_interval_seconds: int = Field(default=600, ge=30)
    cleanup_interval_seconds: int = Field(default=3600, ge=60)
    ui_poll_interval_seconds: int = Field(default=20, ge=2, le=300)
    stale_grace_seconds: int = Field(default=120, ge=0)
    manual_refresh_min_interval_seconds: int = Field(default=10, ge=0)
    scheduler_tick_seconds: float = Field(default=5.0, gt=0)
    scheduler_enabled: bool = True

    retention_hours: int = Field(default=24, ge=1)
    error_retention_days: int = Field(default=7, ge=1)
    work_window_days: int = Field(default=30, ge=1, le=365)
    activity_days: int = Field(default=7, ge=1, le=31)
    timezone: str = "UTC"

    @field_validator("gitlab_url")
    @classmethod
    def _normalize_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if value and not value.startswith(("http://", "https://")):
            raise ValueError("gitlab_url must start with http:// or https://")
        return value

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        value = value.strip().upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"unsupported log level: {value}")
        return value

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def resolve_token(self) -> SecretStr | None:
        """Return the GitLab token: secret file first, environment variable second."""
        candidates = [self.gitlab_token_file] if self.gitlab_token_file else [DEFAULT_SECRET_FILE]
        for path in candidates:
            if path.is_file():
                token = path.read_text(encoding="utf-8").strip()
                if token:
                    return SecretStr(token)
        if self.gitlab_token and self.gitlab_token.get_secret_value().strip():
            return SecretStr(self.gitlab_token.get_secret_value().strip())
        return None

    @property
    def gitlab_configured(self) -> bool:
        return bool(self.gitlab_url) and self.resolve_token() is not None

    @property
    def tls_verify(self) -> bool | str:
        """Value for httpx ``verify``: a CA bundle path, or a boolean."""
        if self.gitlab_ca_bundle:
            return str(self.gitlab_ca_bundle)
        return self.gitlab_verify_tls

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path}"
