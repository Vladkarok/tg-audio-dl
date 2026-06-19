"""Tests for src/main.py — written first (TDD RED phase)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import TimedOut

from src.main import (
    _error_handler,
    _heartbeat_age,
    _heartbeat_loop,
    _heartbeat_probe,
    _record_heartbeat,
    _start_heartbeat,
    build_application,
    post_init,
    setup_logging,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_settings(
    token: str = "123456:ABC-DEF",
    local_server_url: str | None = None,
    cache_dir: Path = Path("/tmp/test_cache"),
    max_file_size_mb: int = 100,
    log_level: str = "INFO",
):
    settings = MagicMock()
    settings.telegram_bot_token = token
    settings.TELEGRAM_BOT_TOKEN = token
    settings.telegram_local_server_url = local_server_url
    settings.TELEGRAM_LOCAL_SERVER_URL = local_server_url
    settings.cache_dir = cache_dir
    settings.CACHE_DIR = cache_dir
    settings.max_file_size_mb = max_file_size_mb
    settings.MAX_FILE_SIZE_MB = max_file_size_mb
    settings.log_level = log_level
    settings.LOG_LEVEL = log_level
    return settings


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------


def test_setup_logging_sets_level(caplog):
    """setup_logging must set the root logger to the requested level."""
    with patch("logging.basicConfig") as mock_basic_cfg:
        setup_logging("DEBUG")
        mock_basic_cfg.assert_called_once()
        call_kwargs = mock_basic_cfg.call_args[1]
        assert call_kwargs["level"] == logging.DEBUG


def test_setup_logging_quiets_httpx():
    """setup_logging must raise httpx logger to WARNING."""
    with patch("logging.basicConfig"):
        setup_logging("INFO")
    assert logging.getLogger("httpx").level == logging.WARNING


def test_setup_logging_quiets_telegram():
    """setup_logging must raise telegram logger to WARNING."""
    with patch("logging.basicConfig"):
        setup_logging("INFO")
    assert logging.getLogger("telegram").level == logging.WARNING


# ---------------------------------------------------------------------------
# build_application — handler registration
# ---------------------------------------------------------------------------


def test_build_application_registers_handlers():
    """build_application must register /start, /help, and URL message handlers."""
    from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

    settings = make_settings()

    with patch("src.main.ApplicationBuilder") as mock_builder_cls:
        # Build a realistic chain: ApplicationBuilder().token(...).post_init(...).build()
        mock_chain = MagicMock()
        mock_builder_cls.return_value = mock_chain
        mock_chain.token.return_value = mock_chain
        mock_chain.read_timeout.return_value = mock_chain
        mock_chain.write_timeout.return_value = mock_chain
        mock_chain.connect_timeout.return_value = mock_chain
        mock_chain.pool_timeout.return_value = mock_chain
        mock_chain.post_init.return_value = mock_chain
        mock_chain.local_mode.return_value = mock_chain
        mock_chain.base_url.return_value = mock_chain
        mock_chain.base_file_url.return_value = mock_chain

        mock_app = MagicMock()
        mock_app.bot_data = {}
        mock_chain.build.return_value = mock_app

        build_application(settings)

    # Verify handlers were added
    assert mock_app.add_handler.call_count == 7
    # A catch-all error handler must be registered (so pool/network errors are
    # logged instead of "No error handlers are registered").
    mock_app.add_error_handler.assert_called_once()

    added_handlers = [call.args[0] for call in mock_app.add_handler.call_args_list]

    command_handlers = [h for h in added_handlers if isinstance(h, CommandHandler)]
    callback_handlers = [
        h for h in added_handlers if isinstance(h, CallbackQueryHandler)
    ]
    message_handlers = [h for h in added_handlers if isinstance(h, MessageHandler)]

    assert len(command_handlers) == 5, (
        "Expected exactly 5 CommandHandlers "
        "(/start, /help, /redownload, /refresh, /chapters)"
    )
    assert len(callback_handlers) == 1, "Expected chapter-page CallbackQueryHandler"
    assert len(message_handlers) == 1, "Expected exactly 1 MessageHandler (URL filter)"

    command_names = {name for h in command_handlers for name in h.commands}
    assert "start" in command_names
    assert "help" in command_names
    assert "redownload" in command_names
    assert "refresh" in command_names
    assert "chapters" in command_names


def test_build_application_stores_settings_in_bot_data():
    """build_application must store settings in app.bot_data['settings']."""
    settings = make_settings()

    with patch("src.main.ApplicationBuilder") as mock_builder_cls:
        mock_chain = MagicMock()
        mock_builder_cls.return_value = mock_chain
        mock_chain.token.return_value = mock_chain
        mock_chain.read_timeout.return_value = mock_chain
        mock_chain.write_timeout.return_value = mock_chain
        mock_chain.connect_timeout.return_value = mock_chain
        mock_chain.pool_timeout.return_value = mock_chain
        mock_chain.post_init.return_value = mock_chain

        mock_app = MagicMock()
        mock_app.bot_data = {}
        mock_chain.build.return_value = mock_app

        build_application(settings)

    assert mock_app.bot_data["settings"] is settings


# ---------------------------------------------------------------------------
# build_application — local server configuration
# ---------------------------------------------------------------------------


def test_build_application_uses_local_server_when_configured():
    """When TELEGRAM_LOCAL_SERVER_URL is set, base_url and local_mode are applied."""
    settings = make_settings(local_server_url="http://localhost:8081")

    with patch("src.main.ApplicationBuilder") as mock_builder_cls:
        mock_chain = MagicMock()
        mock_builder_cls.return_value = mock_chain
        mock_chain.token.return_value = mock_chain
        mock_chain.read_timeout.return_value = mock_chain
        mock_chain.write_timeout.return_value = mock_chain
        mock_chain.connect_timeout.return_value = mock_chain
        mock_chain.pool_timeout.return_value = mock_chain
        mock_chain.base_url.return_value = mock_chain
        mock_chain.base_file_url.return_value = mock_chain
        mock_chain.local_mode.return_value = mock_chain
        mock_chain.post_init.return_value = mock_chain

        mock_app = MagicMock()
        mock_app.bot_data = {}
        mock_chain.build.return_value = mock_app

        build_application(settings)

    mock_chain.base_url.assert_called_once_with("http://localhost:8081/bot")
    mock_chain.base_file_url.assert_called_once_with("http://localhost:8081/file/bot")
    mock_chain.local_mode.assert_called_once_with(True)


def test_build_application_no_local_server_by_default():
    """When TELEGRAM_LOCAL_SERVER_URL is None, local_mode is never called."""
    settings = make_settings(local_server_url=None)

    with patch("src.main.ApplicationBuilder") as mock_builder_cls:
        mock_chain = MagicMock()
        mock_builder_cls.return_value = mock_chain
        mock_chain.token.return_value = mock_chain
        mock_chain.read_timeout.return_value = mock_chain
        mock_chain.write_timeout.return_value = mock_chain
        mock_chain.connect_timeout.return_value = mock_chain
        mock_chain.pool_timeout.return_value = mock_chain
        mock_chain.post_init.return_value = mock_chain

        mock_app = MagicMock()
        mock_app.bot_data = {}
        mock_chain.build.return_value = mock_app

        build_application(settings)

    mock_chain.local_mode.assert_not_called()
    mock_chain.base_url.assert_not_called()


# ---------------------------------------------------------------------------
# post_init — dependency wiring
# ---------------------------------------------------------------------------


async def test_post_init_creates_cache_dir(tmp_path):
    """post_init must create settings.cache_dir and the tmp subdirectory."""
    cache_dir = tmp_path / "cache"
    settings = make_settings(cache_dir=cache_dir)

    mock_app = MagicMock()
    mock_app.bot_data = {"settings": settings}

    with (
        patch("src.main.create_cache", return_value=MagicMock()),
        patch("src.main.AudioDownloader", return_value=MagicMock()),
        patch("src.main._start_heartbeat"),
    ):
        await post_init(mock_app)

    assert cache_dir.exists(), "cache_dir must be created by post_init"
    assert (cache_dir / "tmp").exists(), "cache_dir/tmp must be created by post_init"


async def test_post_init_wires_dependencies(tmp_path):
    """post_init must store 'cache' and 'downloader' into bot_data."""
    cache_dir = tmp_path / "cache"
    settings = make_settings(cache_dir=cache_dir)

    mock_app = MagicMock()
    mock_app.bot_data = {"settings": settings}

    fake_cache = MagicMock()
    fake_downloader = MagicMock()

    with (
        patch("src.main.create_cache", return_value=fake_cache) as mock_create_cache,
        patch(
            "src.main.AudioDownloader", return_value=fake_downloader
        ) as mock_downloader_cls,
        patch("src.main._start_heartbeat"),
    ):
        await post_init(mock_app)

    # Cache was created from settings
    mock_create_cache.assert_called_once_with(settings)
    assert mock_app.bot_data["cache"] is fake_cache

    # Downloader was created with correct args
    mock_downloader_cls.assert_called_once()
    call_kwargs = mock_downloader_cls.call_args[1]
    assert call_kwargs["download_dir"] == cache_dir / "tmp"
    assert call_kwargs["max_file_size_bytes"] == settings.max_file_size_mb * 1024 * 1024
    assert mock_app.bot_data["downloader"] is fake_downloader


async def test_post_init_uses_settings_token_attribute(tmp_path):
    """post_init reads settings from bot_data — not from get_settings()."""
    cache_dir = tmp_path / "cache"
    settings = make_settings(cache_dir=cache_dir)

    mock_app = MagicMock()
    mock_app.bot_data = {"settings": settings}

    with (
        patch("src.main.create_cache", return_value=MagicMock()),
        patch("src.main.AudioDownloader", return_value=MagicMock()),
        patch("src.main.get_settings") as mock_get_settings,
        patch("src.main._start_heartbeat"),
    ):
        await post_init(mock_app)

    # post_init must NOT call get_settings() — it reads from bot_data
    mock_get_settings.assert_not_called()


# ---------------------------------------------------------------------------
# build_application — pool timeout + error handler
# ---------------------------------------------------------------------------


def test_build_application_sets_pool_timeout():
    """build_application must apply settings.POOL_TIMEOUT_SECONDS to the builder."""
    settings = make_settings()
    settings.POOL_TIMEOUT_SECONDS = 12.5

    with patch("src.main.ApplicationBuilder") as mock_builder_cls:
        mock_chain = MagicMock()
        mock_builder_cls.return_value = mock_chain
        for attr in (
            "token",
            "read_timeout",
            "write_timeout",
            "connect_timeout",
            "pool_timeout",
            "post_init",
        ):
            getattr(mock_chain, attr).return_value = mock_chain
        mock_app = MagicMock()
        mock_app.bot_data = {}
        mock_chain.build.return_value = mock_app

        build_application(settings)

    mock_chain.pool_timeout.assert_called_once_with(12.5)


async def test_error_handler_logs_the_error(caplog):
    """_error_handler logs context.error instead of swallowing it."""
    context = MagicMock()
    context.error = RuntimeError("kaboom")

    with caplog.at_level(logging.ERROR):
        await _error_handler(object(), context)

    assert "Unhandled error" in caplog.text


# ---------------------------------------------------------------------------
# Heartbeat watchdog
# ---------------------------------------------------------------------------


class TestHeartbeat:
    async def test_probe_returns_true_on_success(self):
        bot = MagicMock()
        bot.get_me = AsyncMock(return_value=MagicMock())
        assert await _heartbeat_probe(bot, timeout=1) is True

    async def test_probe_returns_false_on_error(self):
        bot = MagicMock()
        bot.get_me = AsyncMock(side_effect=TimedOut("pool exhausted"))
        assert await _heartbeat_probe(bot, timeout=1) is False

    def test_record_heartbeat_writes_timestamp(self, tmp_path, monkeypatch):
        hb = tmp_path / "heartbeat"
        monkeypatch.setattr("src.main.HEARTBEAT_PATH", hb)
        _record_heartbeat()
        assert float(hb.read_text()) > 0

    def test_heartbeat_age_none_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.main.HEARTBEAT_PATH", tmp_path / "absent")
        assert _heartbeat_age() is None

    def test_heartbeat_age_measures_staleness(self, tmp_path, monkeypatch):
        hb = tmp_path / "heartbeat"
        hb.write_text("x")
        monkeypatch.setattr("src.main.HEARTBEAT_PATH", hb)
        stale = time.time() - 200
        os.utime(hb, (stale, stale))
        age = _heartbeat_age()
        assert age is not None and age > 150

    async def test_loop_records_while_reachable(self, monkeypatch):
        """While getMe succeeds, the heartbeat is refreshed each cycle."""
        bot = MagicMock()
        bot.get_me = AsyncMock(return_value=MagicMock())
        record_calls = {"n": 0}

        def fake_record():
            record_calls["n"] += 1
            if record_calls["n"] >= 3:
                raise RuntimeError("stop loop")

        monkeypatch.setattr("src.main._record_heartbeat", fake_record)

        with pytest.raises(RuntimeError, match="stop loop"):
            await _heartbeat_loop(bot, interval=0, probe_timeout=1)

        assert record_calls["n"] == 3

    async def test_loop_does_not_record_on_failure(self, monkeypatch):
        """A failed probe must NOT refresh the heartbeat, so it goes stale and
        the off-loop watchdog can act."""
        bot = MagicMock()
        bot.get_me = AsyncMock(side_effect=TimedOut("pool exhausted"))
        record = MagicMock()
        monkeypatch.setattr("src.main._record_heartbeat", record)

        task = asyncio.create_task(_heartbeat_loop(bot, interval=0, probe_timeout=1))
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        record.assert_not_called()
        assert bot.get_me.call_count >= 1

    async def test_start_heartbeat_schedules_task_and_watchdog(self, monkeypatch):
        async def fake_loop(*_args, **_kwargs):
            return None

        monkeypatch.setattr("src.main._heartbeat_loop", fake_loop)
        monkeypatch.setattr("src.main._record_heartbeat", lambda: None)
        thread_cls = MagicMock()
        monkeypatch.setattr("src.main.threading.Thread", thread_cls)

        app = MagicMock()
        app.bot_data = {}
        settings = make_settings()
        settings.HEARTBEAT_INTERVAL_SECONDS = 30
        settings.HEARTBEAT_PROBE_TIMEOUT_SECONDS = 20
        settings.HEARTBEAT_MAX_FAILURES = 3

        _start_heartbeat(app, settings)

        task = app.bot_data["_heartbeat_task"]
        assert task is not None
        await task

        # Off-loop watchdog must be started as a daemon thread — this is what
        # recovers a fully wedged event loop.
        thread_cls.assert_called_once()
        assert thread_cls.call_args.kwargs.get("daemon") is True
        thread_cls.return_value.start.assert_called_once()
        # Deadline = interval*max_failures + probe_timeout; checks every interval.
        assert thread_cls.call_args.kwargs.get("args") == (30 * 3 + 20, 30)
