"""Tests for src/main.py — written first (TDD RED phase)."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.main import build_application, post_init, setup_logging

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
    ):
        await post_init(mock_app)

    # post_init must NOT call get_settings() — it reads from bot_data
    mock_get_settings.assert_not_called()
