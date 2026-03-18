import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, EnvSettingsSource, SettingsConfigDict


class _CommaSeparatedEnvSource(EnvSettingsSource):
    """Custom env source that treats ALLOWED_USER_IDS as a plain string
    instead of attempting JSON decoding on comma-separated values."""

    def prepare_field_value(
        self, field_name: str, field: Any, value: Any, value_is_complex: bool
    ) -> Any:
        if field_name == "ALLOWED_USER_IDS" and isinstance(value, str):
            # Return raw string; pydantic model_validator will parse it
            return value
        return super().prepare_field_value(field_name, field, value, value_is_complex)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    TELEGRAM_BOT_TOKEN: str
    TELEGRAM_LOCAL_SERVER_URL: str | None = None

    CACHE_DIR: Path = Path("./cache")
    CACHE_MAX_SIZE_GB: float = 5.0

    S3_ENABLED: bool = False
    S3_BUCKET: str | None = None
    AWS_ACCESS_KEY_ID: str | None = None
    AWS_SECRET_ACCESS_KEY: str | None = None
    AWS_REGION: str = "us-east-1"

    PROXY_URL: str | None = None

    MAX_FILE_SIZE_MB: int = 2000
    ALLOWED_USER_IDS: list[int] = []
    LOG_LEVEL: str = "INFO"
    PLAYLIST_MAX_TRACKS: int = 50
    RATE_LIMIT_PER_MINUTE: int = 5
    DOWNLOAD_TIMEOUT_SECONDS: int = 1800

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: Any,
        env_settings: Any,
        dotenv_settings: Any,
        file_secret_settings: Any,
    ) -> tuple[Any, ...]:
        return (
            init_settings,
            _CommaSeparatedEnvSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )

    @field_validator(
        "TELEGRAM_LOCAL_SERVER_URL",
        "PROXY_URL",
        "S3_BUCKET",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        mode="before",
    )
    @classmethod
    def empty_str_to_none(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @field_validator("ALLOWED_USER_IDS", mode="before")
    @classmethod
    def parse_allowed_user_ids(cls, v: Any) -> list[int]:
        if v is None or v == "":
            return []
        if isinstance(v, str):
            stripped = v.strip()
            if stripped == "":
                return []
            # Handle JSON array syntax: [] or [123, 456]
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                    if isinstance(parsed, list):
                        return [int(uid) for uid in parsed]
                except (json.JSONDecodeError, ValueError):
                    pass
            return [int(uid.strip()) for uid in stripped.split(",") if uid.strip()]
        if isinstance(v, list):
            return [int(uid) for uid in v]
        return []

    @field_validator("CACHE_MAX_SIZE_GB")
    @classmethod
    def validate_cache_max_size(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("CACHE_MAX_SIZE_GB must be positive")
        return v

    @field_validator("RATE_LIMIT_PER_MINUTE")
    @classmethod
    def validate_rate_limit(cls, v: int) -> int:
        if v < 1:
            raise ValueError("RATE_LIMIT_PER_MINUTE must be at least 1")
        return v

    @field_validator("DOWNLOAD_TIMEOUT_SECONDS")
    @classmethod
    def validate_download_timeout(cls, v: int) -> int:
        if v < 1:
            raise ValueError("DOWNLOAD_TIMEOUT_SECONDS must be at least 1")
        return v

    @field_validator("LOG_LEVEL")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"LOG_LEVEL must be one of {valid}")
        return upper

    @field_validator("TELEGRAM_LOCAL_SERVER_URL")
    @classmethod
    def validate_local_server_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.startswith(("http://", "https://")):
            raise ValueError(
                "TELEGRAM_LOCAL_SERVER_URL must start with http:// or https://"
            )
        return v

    @field_validator("PROXY_URL")
    @classmethod
    def validate_proxy_url(cls, v: str | None) -> str | None:
        if v is None:
            return v
        valid_schemes = ("http://", "https://", "socks4://", "socks5://")
        if not v.startswith(valid_schemes):
            raise ValueError(
                "PROXY_URL must start with http://, https://, socks4://, or socks5://"
            )
        return v

    @field_validator("TELEGRAM_BOT_TOKEN")
    @classmethod
    def validate_bot_token(cls, v: str) -> str:
        if not v or v.strip() == "":
            raise ValueError("TELEGRAM_BOT_TOKEN must be set to a real token")
        return v

    @model_validator(mode="after")
    def validate_s3_config(self) -> "Settings":
        if self.S3_ENABLED:
            if not self.S3_BUCKET:
                raise ValueError("S3_BUCKET must be set when S3_ENABLED=True")
            missing = []
            if not self.AWS_ACCESS_KEY_ID:
                missing.append("AWS_ACCESS_KEY_ID")
            if not self.AWS_SECRET_ACCESS_KEY:
                missing.append("AWS_SECRET_ACCESS_KEY")
            if missing:
                raise ValueError(
                    f"{', '.join(missing)} must be set when S3_ENABLED=True"
                )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
