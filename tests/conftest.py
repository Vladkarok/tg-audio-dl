"""Shared pytest fixtures and configuration."""

import asyncio

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


@pytest.fixture(autouse=True)
def patch_blocking_io_helpers(monkeypatch, request):
    """Keep tests deterministic without relying on real executor teardown.

    In this environment, executor-backed asyncio helpers can leave pytest
    hanging during loop shutdown. Most tests only need blocking work to happen
    eventually, not on a real worker thread.
    """
    from src.cache import s3 as s3_module
    from src.downloader import client as downloader_module

    async def _inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    async def _delayed(func, /, *args, **kwargs):
        await asyncio.sleep(5)
        return func(*args, **kwargs)

    helper = (
        _delayed
        if "tests/test_downloader.py::TestDownloadTimeout::" in request.node.nodeid
        else _inline
    )

    monkeypatch.setattr(s3_module, "_run_blocking", helper)
    monkeypatch.setattr(downloader_module, "_run_blocking", helper)
