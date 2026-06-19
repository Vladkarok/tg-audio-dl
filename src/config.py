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
    COOKIES_FILE: str | None = None

    MAX_FILE_SIZE_MB: int = 2000
    ALLOWED_USER_IDS: list[int] = []
    LOG_LEVEL: str = "INFO"
    PLAYLIST_MAX_TRACKS: int = 50
    RATE_LIMIT_PER_MINUTE: int = 5
    DOWNLOAD_TIMEOUT_SECONDS: int = 1800
    TMP_MAX_AGE_SECONDS: int = 3600
    TMP_CLEANUP_INTERVAL_SECONDS: int = 900
    EXPERIMENTAL_CHAPTER_PAGES_ENABLED: bool = False

    # Telegram HTTPX connection pool: how long a request waits for a free
    # connection before failing instead of blocking forever.
    POOL_TIMEOUT_SECONDS: float = 20.0

    # Liveness watchdog. The bot periodically proves it can reach Telegram
    # (getMe) and refreshes a heartbeat file the Docker healthcheck reads.
    # After this many consecutive failures it force-exits so the container
    # restart policy can recover a wedged event loop / exhausted pool.
    HEARTBEAT_INTERVAL_SECONDS: int = 30
    HEARTBEAT_PROBE_TIMEOUT_SECONDS: int = 20
    HEARTBEAT_MAX_FAILURES: int = 3

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
        "COOKIES_FILE",
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

    @field_validator("TMP_MAX_AGE_SECONDS")
    @classmethod
    def validate_tmp_max_age(cls, v: int) -> int:
        if v < 60:
            raise ValueError("TMP_MAX_AGE_SECONDS must be at least 60")
        return v

    @field_validator("TMP_CLEANUP_INTERVAL_SECONDS")
    @classmethod
    def validate_tmp_cleanup_interval(cls, v: int) -> int:
        if v < 60:
            raise ValueError("TMP_CLEANUP_INTERVAL_SECONDS must be at least 60")
        return v

    @field_validator("POOL_TIMEOUT_SECONDS")
    @classmethod
    def validate_pool_timeout(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("POOL_TIMEOUT_SECONDS must be positive")
        return v

    @field_validator("HEARTBEAT_INTERVAL_SECONDS")
    @classmethod
    def validate_heartbeat_interval(cls, v: int) -> int:
        if v < 5:
            raise ValueError("HEARTBEAT_INTERVAL_SECONDS must be at least 5")
        return v

    @field_validator("HEARTBEAT_PROBE_TIMEOUT_SECONDS")
    @classmethod
    def validate_heartbeat_probe_timeout(cls, v: int) -> int:
        if v < 1:
            raise ValueError("HEARTBEAT_PROBE_TIMEOUT_SECONDS must be at least 1")
        return v

    @field_validator("HEARTBEAT_MAX_FAILURES")
    @classmethod
    def validate_heartbeat_max_failures(cls, v: int) -> int:
        if v < 1:
            raise ValueError("HEARTBEAT_MAX_FAILURES must be at least 1")
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
        if self.S3_ENABLED and not self.S3_BUCKET:
            raise ValueError("S3_BUCKET must be set when S3_ENABLED=True")
        # AWS keys are optional — boto3 falls back to its credential chain
        # (IAM roles, instance profiles, env vars, ~/.aws/credentials, etc.)
        return self

    @model_validator(mode="after")
    def validate_tmp_age_vs_timeout(self) -> "Settings":
        if self.TMP_MAX_AGE_SECONDS <= self.DOWNLOAD_TIMEOUT_SECONDS:
            raise ValueError(
                f"TMP_MAX_AGE_SECONDS ({self.TMP_MAX_AGE_SECONDS}) must be greater "
                f"than DOWNLOAD_TIMEOUT_SECONDS ({self.DOWNLOAD_TIMEOUT_SECONDS}) "
                "to avoid deleting files that are still being downloaded"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
