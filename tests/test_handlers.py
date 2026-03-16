"""Tests for src/bot/handlers.py — written first (TDD RED phase)."""

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

import pytest

from src.bot.handlers import _user_request_times, handle_help, handle_start, handle_url
from src.downloader.client import (
    DownloadError,
    DownloadResult,
    FileTooLargeError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_download_result(video_id="dQw4w9WgXcQ", title="Test Song"):
    return DownloadResult(
        file_path=Path(f"/tmp/{video_id}.m4a"),
        video_id=video_id,
        title=title,
        artist="Test Artist",
        duration_seconds=180,
        thumbnail_url=None,
        file_size_bytes=1024 * 1024,
    )


def make_update(user_id=12345, text="https://youtu.be/dQw4w9WgXcQ"):
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.chat_id = 999
    update.message.message_id = 1
    update.message.reply_text = AsyncMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    return update


def make_context(
    settings=None,
    downloader=None,
    cache=None,
    allowed_users=None,
    rate_limit=5,
):
    from src.config import Settings

    if settings is None:
        settings = MagicMock(spec=Settings)
        settings.ALLOWED_USER_IDS = allowed_users if allowed_users is not None else []
        settings.PLAYLIST_MAX_TRACKS = 50
        settings.MAX_FILE_SIZE_MB = 2000
        settings.RATE_LIMIT_PER_MINUTE = rate_limit

    if downloader is None:
        downloader = MagicMock()
        downloader.download = AsyncMock(return_value=[make_download_result()])

    if cache is None:
        cache = MagicMock()
        cache.exists = AsyncMock(return_value=False)
        cache.get = AsyncMock(return_value=None)
        cache.put = AsyncMock(return_value=Path("/tmp/dQw4w9WgXcQ.m4a"))
        cache.get_file_id = AsyncMock(return_value=None)
        cache.store_file_id = AsyncMock()

    context = MagicMock()
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock(return_value=MagicMock(message_id=77))
    context.bot.edit_message_text = AsyncMock()
    context.bot.delete_message = AsyncMock()
    context.bot.send_audio = AsyncMock()
    context.bot_data = {
        "settings": settings,
        "downloader": downloader,
        "cache": cache,
    }
    return context


# ---------------------------------------------------------------------------
# Start / Help handlers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_rate_limit_state():
    """Reset the in-memory rate-limit dict before every test."""
    _user_request_times.clear()
    yield
    _user_request_times.clear()


class TestStartHandler:
    async def test_handle_start_replies(self):
        update = make_update(text="/start")
        context = make_context()
        await handle_start(update, context)
        update.message.reply_text.assert_called_once()
        call_args = update.message.reply_text.call_args
        text = call_args.args[0] if call_args.args else call_args.kwargs.get("text", "")
        assert len(text) > 0

    async def test_handle_help_replies(self):
        update = make_update(text="/help")
        context = make_context()
        await handle_help(update, context)
        update.message.reply_text.assert_called_once()


# ---------------------------------------------------------------------------
# URL handler — happy paths
# ---------------------------------------------------------------------------


class TestHandleUrlCacheHit:
    async def test_handle_url_cache_hit_sends_audio(self, tmp_path):
        """Cache hit: skips download and sends audio."""
        cached_file = tmp_path / "dQw4w9WgXcQ.m4a"
        cached_file.write_bytes(b"fake audio data")

        update = make_update()
        cache = MagicMock()
        cache.exists = AsyncMock(return_value=True)
        cache.get = AsyncMock(return_value=cached_file)
        cache.get_file_id = AsyncMock(return_value=None)
        cache.store_file_id = AsyncMock()
        downloader = MagicMock()
        downloader.download = AsyncMock()

        context = make_context(cache=cache, downloader=downloader)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await handle_url(update, context)

        # Download must NOT have been called
        downloader.download.assert_not_called()
        # Audio must have been sent
        context.bot.send_audio.assert_called_once()


class TestHandleUrlFileIdCaching:
    async def test_cache_hit_with_file_id_sends_by_file_id(self, tmp_path):
        """Cache hit with stored file_id: bot.send_audio called with audio=file_id string."""
        update = make_update()
        cache = MagicMock()
        cache.exists = AsyncMock(return_value=True)
        cache.get_file_id = AsyncMock(return_value="AgACAgIA_cached_file_id")
        cache.get = AsyncMock(return_value=tmp_path / "dQw4w9WgXcQ.m4a")
        cache.store_file_id = AsyncMock()

        context = make_context(cache=cache)
        context.bot.send_audio = AsyncMock(
            return_value=MagicMock(audio=MagicMock(file_id="AgACAgIA_cached_file_id"))
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await handle_url(update, context)

        # send_audio should be called with audio= as a string (file_id)
        context.bot.send_audio.assert_called_once()
        call_kwargs = context.bot.send_audio.call_args
        audio_arg = call_kwargs.kwargs.get("audio") or (
            call_kwargs.args[0] if call_kwargs.args else None
        )
        assert audio_arg == "AgACAgIA_cached_file_id"

    async def test_cache_hit_no_file_id_stores_after_upload(self, tmp_path):
        """Cache hit without file_id: file_id stored after successful upload."""
        cached_file = tmp_path / "dQw4w9WgXcQ.m4a"
        cached_file.write_bytes(b"fake audio data")

        update = make_update()
        cache = MagicMock()
        cache.exists = AsyncMock(return_value=True)
        cache.get = AsyncMock(return_value=cached_file)
        cache.get_file_id = AsyncMock(return_value=None)
        cache.store_file_id = AsyncMock()

        context = make_context(cache=cache)
        msg_mock = MagicMock()
        msg_mock.audio = MagicMock()
        msg_mock.audio.file_id = "new_fid_from_upload"
        context.bot.send_audio = AsyncMock(return_value=msg_mock)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await handle_url(update, context)

        cache.store_file_id.assert_called_once_with(
            "dQw4w9WgXcQ", "new_fid_from_upload"
        )

    async def test_cache_miss_stores_file_id_after_upload(self, tmp_path):
        """Cache miss: file_id stored after successful download+upload."""
        audio_file = tmp_path / "dQw4w9WgXcQ.m4a"
        audio_file.write_bytes(b"audio")

        result = DownloadResult(
            file_path=audio_file,
            video_id="dQw4w9WgXcQ",
            title="Test Song",
            artist="Test Artist",
            duration_seconds=180,
            thumbnail_url=None,
            file_size_bytes=5,
        )

        update = make_update()
        cache = MagicMock()
        cache.exists = AsyncMock(return_value=False)
        cache.put = AsyncMock(return_value=audio_file)
        cache.store_file_id = AsyncMock()

        downloader = MagicMock()
        downloader.download = AsyncMock(return_value=[result])

        context = make_context(cache=cache, downloader=downloader)
        msg_mock = MagicMock()
        msg_mock.audio = MagicMock()
        msg_mock.audio.file_id = "new_fid_after_download"
        context.bot.send_audio = AsyncMock(return_value=msg_mock)

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await handle_url(update, context)

        cache.store_file_id.assert_called_once_with(
            "dQw4w9WgXcQ", "new_fid_after_download"
        )


class TestHandleUrlCacheHitUX:
    async def test_cache_hit_shows_found_in_cache(self, tmp_path):
        """Cache hit: progress shows 'Found in cache' on DOWNLOADING DONE step."""
        cached_file = tmp_path / "dQw4w9WgXcQ.m4a"
        cached_file.write_bytes(b"fake audio data")

        update = make_update()
        cache = MagicMock()
        cache.exists = AsyncMock(return_value=True)
        cache.get = AsyncMock(return_value=cached_file)
        cache.get_file_id = AsyncMock(return_value=None)
        cache.store_file_id = AsyncMock()

        context = make_context(cache=cache)
        context.bot.send_audio = AsyncMock(
            return_value=MagicMock(audio=MagicMock(file_id="new_fid"))
        )

        set_step_calls = []
        original_set_step = None

        async def capture_set_step(step, status, detail=""):
            set_step_calls.append((step, status, detail))
            await original_set_step(step, status, detail)

        from src.bot.progress import ProgressManager

        original_create = ProgressManager.create

        async def patched_create(self):
            nonlocal original_set_step
            original_set_step = self.set_step
            self.set_step = lambda step, status, detail="": capture_set_step(
                step, status, detail
            )
            return await original_create(self)

        with (
            patch.object(ProgressManager, "create", patched_create),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await handle_url(update, context)

        from src.bot.progress import Step, StepStatus

        # Must have called set_step with DOWNLOADING, DONE, "Found in cache"
        assert any(
            step == Step.DOWNLOADING
            and status == StepStatus.DONE
            and detail == "Found in cache"
            for step, status, detail in set_step_calls
        ), f"Expected DOWNLOADING DONE 'Found in cache' in {set_step_calls}"


class TestHandleUrlCacheMiss:
    async def test_handle_url_cache_miss_downloads_and_sends(self):
        """Cache miss: downloads then sends audio."""
        update = make_update()
        context = make_context()

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch("pathlib.Path.open", mock_open(read_data=b"fake audio data")),
        ):
            await handle_url(update, context)

        context.bot_data["downloader"].download.assert_called_once()
        context.bot.send_audio.assert_called_once()

    async def test_handle_url_deletes_progress_after_send(self):
        """Progress message is deleted after successful send."""
        update = make_update()
        context = make_context()

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch("pathlib.Path.open", mock_open(read_data=b"fake audio data")),
        ):
            await handle_url(update, context)

        context.bot.delete_message.assert_called_once()


# ---------------------------------------------------------------------------
# URL handler — error / edge cases
# ---------------------------------------------------------------------------


class TestHandleUrlErrors:
    async def test_handle_url_invalid_url_ignored(self):
        """Non-YouTube text in message is silently ignored."""
        update = make_update(text="just some random text")
        context = make_context()
        await handle_url(update, context)
        # No download, no audio, no progress message
        context.bot_data["downloader"].download.assert_not_called()
        context.bot.send_audio.assert_not_called()

    async def test_handle_url_download_error_shows_error(self):
        """DownloadError: progress shows error step, message is NOT deleted."""
        update = make_update()
        downloader = MagicMock()
        downloader.download = AsyncMock(side_effect=DownloadError("unavailable"))
        context = make_context(downloader=downloader)

        await handle_url(update, context)

        # Progress should NOT be deleted
        context.bot.delete_message.assert_not_called()
        # Error edit should have been attempted
        context.bot.edit_message_text.assert_called()

    async def test_handle_url_file_too_large_informs_user(self):
        """FileTooLargeError results in a user-friendly edit."""
        update = make_update()
        downloader = MagicMock()
        downloader.download = AsyncMock(
            side_effect=FileTooLargeError(300 * 1024 * 1024, 200 * 1024 * 1024)
        )
        context = make_context(downloader=downloader)

        await handle_url(update, context)

        context.bot.edit_message_text.assert_called()


# ---------------------------------------------------------------------------
# URL handler — playlist
# ---------------------------------------------------------------------------


class TestHandleUrlPlaylist:
    async def test_handle_url_playlist_sends_multiple(self):
        """Playlist result: send_audio called once per track."""
        update = make_update(
            text="https://www.youtube.com/playlist?list=PLrEnWoR732-BHrPp_Pm8_VleD68f9s14-"
        )
        results = [
            make_download_result(video_id=f"vid{i:09d}", title=f"Track {i}")
            for i in range(3)
        ]
        downloader = MagicMock()
        downloader.download = AsyncMock(return_value=results)
        context = make_context(downloader=downloader)

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch("pathlib.Path.open", mock_open(read_data=b"fake audio data")),
        ):
            await handle_url(update, context)

        assert context.bot.send_audio.call_count == 3


# ---------------------------------------------------------------------------
# URL handler — access control
# ---------------------------------------------------------------------------


class TestHandleUrlAccessControl:
    async def test_handle_url_respects_allowed_users(self):
        """If ALLOWED_USER_IDS is set, unknown users get no response."""
        update = make_update(user_id=99999)
        context = make_context(allowed_users=[11111, 22222])

        await handle_url(update, context)

        context.bot_data["downloader"].download.assert_not_called()
        context.bot.send_audio.assert_not_called()

    async def test_handle_url_allowed_user_can_download(self):
        """Users in ALLOWED_USER_IDS can download."""
        update = make_update(user_id=12345)
        context = make_context(allowed_users=[12345])

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch("pathlib.Path.open", mock_open(read_data=b"fake audio data")),
        ):
            await handle_url(update, context)

        context.bot.send_audio.assert_called_once()


# ---------------------------------------------------------------------------
# URL handler — rate limiting
# ---------------------------------------------------------------------------


class TestHandleUrlRateLimit:
    async def test_handle_url_rate_limit_blocks(self):
        """6th request within 1 minute from same user gets rate-limit reply."""
        user_id = 55555
        now = time.monotonic()
        # Pre-fill with 5 timestamps within the last minute
        _user_request_times[user_id] = [
            now - 10,
            now - 20,
            now - 30,
            now - 40,
            now - 50,
        ]

        update = make_update(user_id=user_id)
        context = make_context(rate_limit=5)

        await handle_url(update, context)

        # Download must NOT run
        context.bot_data["downloader"].download.assert_not_called()
        # A message must be sent back to inform the user
        update.message.reply_text.assert_called_once()
        msg = update.message.reply_text.call_args.args[0]
        assert "rate" in msg.lower() or "limit" in msg.lower() or "wait" in msg.lower()

    async def test_handle_url_rate_limit_allows_after_window(self):
        """Old requests outside the 60-second window are not counted."""
        user_id = 66666
        now = time.monotonic()
        # All 5 timestamps are >60 seconds ago — should NOT be counted
        _user_request_times[user_id] = [
            now - 70,
            now - 80,
            now - 90,
            now - 100,
            now - 110,
        ]

        update = make_update(user_id=user_id)
        context = make_context(rate_limit=5)

        with (
            patch("asyncio.sleep", new_callable=AsyncMock),
            patch("pathlib.Path.open", mock_open(read_data=b"fake audio data")),
        ):
            await handle_url(update, context)

        context.bot.send_audio.assert_called_once()
