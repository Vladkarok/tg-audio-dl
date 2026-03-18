"""Shared pytest fixtures and configuration."""

import pytest


@pytest.fixture(autouse=True)
def clear_env_vars(monkeypatch):
    """Remove any real environment variables that could interfere with config tests."""
    vars_to_clear = [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_LOCAL_SERVER_URL",
        "CACHE_DIR",
        "CACHE_MAX_SIZE_GB",
        "S3_ENABLED",
        "S3_BUCKET",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_REGION",
        "MAX_FILE_SIZE_MB",
        "ALLOWED_USER_IDS",
        "LOG_LEVEL",
        "PLAYLIST_MAX_TRACKS",
        "RATE_LIMIT_PER_MINUTE",
        "PROXY_URL",
        "DOWNLOAD_TIMEOUT_SECONDS",
        "TMP_MAX_AGE_SECONDS",
        "TMP_CLEANUP_INTERVAL_SECONDS",
    ]
    for var in vars_to_clear:
        monkeypatch.delenv(var, raising=False)
