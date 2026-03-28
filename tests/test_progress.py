"""Tests for src/bot/progress.py — written first (TDD RED phase)."""

from unittest.mock import AsyncMock, MagicMock

from telegram.error import BadRequest

from src.bot.progress import ProgressManager, Step, StepStatus


def make_bot():
    """Create a mock bot with the methods ProgressManager needs."""
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    bot.edit_message_text = AsyncMock()
    bot.delete_message = AsyncMock()
    return bot


class TestRender:
    def test_render_initial_state(self):
        """All steps PENDING except RECEIVED which is DONE."""
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=1, reply_to_message_id=10)
        text = pm.render()

        # RECEIVED is done
        assert "Link received" in text
        # Other steps pending
        assert "Downloading audio" in text
        assert "Processing" in text
        assert "Uploading to Telegram" in text
        # Header present
        assert "Processing your request" in text

    def test_render_downloading_with_percentage(self):
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=1, reply_to_message_id=10)
        pm._steps[Step.DOWNLOADING] = (StepStatus.ACTIVE, "67%")
        text = pm.render()
        assert "67%" in text
        assert "Downloading audio" in text

    def test_render_all_done(self):
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=1, reply_to_message_id=10)
        for step in Step:
            pm._steps[step] = (StepStatus.DONE, "")
        text = pm.render()
        # All steps should show done icon
        done_icon = "✅"
        assert text.count(done_icon) == len(Step)

    def test_render_error_step(self):
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=1, reply_to_message_id=10)
        pm._steps[Step.DOWNLOADING] = (StepStatus.ERROR, "")
        text = pm.render()
        assert "❌" in text

    def test_render_playlist_context(self):
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=1, reply_to_message_id=10)
        pm._playlist_track = 3
        pm._playlist_total = 12
        text = pm.render()
        assert "3" in text
        assert "12" in text
        assert "Playlist" in text

    def test_render_playlist_context_with_title(self):
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=1, reply_to_message_id=10)
        pm._playlist_track = 2
        pm._playlist_total = 8
        pm._playlist_track_title = "My Awesome Song"
        text = pm.render()
        assert "My Awesome Song" in text
        assert "📄" in text
        assert "2" in text
        assert "8" in text

    def test_render_playlist_context_no_title_omits_title_line(self):
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=1, reply_to_message_id=10)
        pm._playlist_track = 1
        pm._playlist_total = 5
        text = pm.render()
        assert "📄" not in text

    def test_render_playlist_context_title_truncated(self):
        """Titles longer than 80 chars are truncated."""
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=1, reply_to_message_id=10)
        pm._playlist_track = 1
        pm._playlist_total = 1
        pm._playlist_track_title = "A" * 100
        text = pm.render()
        assert "A" * 80 in text
        assert "A" * 81 not in text

    def test_render_no_percentage(self):
        """DOWNLOADING ACTIVE with no detail shows no percentage."""
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=1, reply_to_message_id=10)
        pm._steps[Step.DOWNLOADING] = (StepStatus.ACTIVE, "")
        text = pm.render()
        assert "Downloading audio" in text
        assert "%" not in text


class TestRenderWithDetail:
    def test_render_done_step_shows_detail(self):
        """A DONE step with detail text renders detail in output."""
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=1, reply_to_message_id=10)
        pm._steps[Step.DOWNLOADING] = (StepStatus.DONE, "Found in cache")
        rendered = pm.render()
        assert "Found in cache" in rendered
        assert "✅" in rendered


class TestProgressManagerAsync:
    async def test_create_sends_initial_message(self):
        """create() sends a message and stores its message_id."""
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=100, reply_to_message_id=5)
        result = await pm.create()
        assert result is pm
        bot.send_message.assert_called_once()
        call_kwargs = bot.send_message.call_args
        assert call_kwargs.kwargs.get("chat_id") == 100 or call_kwargs.args[0] == 100

    async def test_set_step_calls_edit_message(self):
        """set_step() edits the existing message."""
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=1, reply_to_message_id=10)
        await pm.create()
        # Reset to be sure about call count
        bot.edit_message_text.reset_mock()

        await pm.set_step(Step.DOWNLOADING, StepStatus.ACTIVE)
        bot.edit_message_text.assert_called_once()

    async def test_debounce_limits_edits_to_one_per_second(self):
        """Rapid successive calls should result in at most 1 edit within 1 second."""
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=1, reply_to_message_id=10)
        await pm.create()
        bot.edit_message_text.reset_mock()

        # Simulate last_edit_time as just-now so debounce triggers
        import time

        pm._last_edit_time = time.monotonic()

        # These calls happen within the debounce window
        await pm.set_step(Step.DOWNLOADING, StepStatus.ACTIVE, "10%")
        await pm.set_step(Step.DOWNLOADING, StepStatus.ACTIVE, "20%")
        await pm.set_step(Step.DOWNLOADING, StepStatus.ACTIVE, "30%")

        # Debounce: only 0 edits (all within 1 second of last_edit_time)
        assert bot.edit_message_text.call_count == 0

    async def test_delete_calls_delete_message(self):
        """delete() calls bot.delete_message with correct ids."""
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=99, reply_to_message_id=10)
        await pm.create()
        pm._message_id = 42

        await pm.delete()
        bot.delete_message.assert_called_once_with(chat_id=99, message_id=42)

    async def test_bad_request_not_modified_ignored(self):
        """edit_message_text raising 'Message is not modified' does not propagate."""
        bot = make_bot()
        bot.edit_message_text = AsyncMock(
            side_effect=BadRequest("Message is not modified")
        )
        pm = ProgressManager(bot, chat_id=1, reply_to_message_id=10)
        await pm.create()
        # Force last_edit_time to be old enough to allow edit
        pm._last_edit_time = 0.0

        # Should NOT raise
        await pm.set_step(Step.DOWNLOADING, StepStatus.ACTIVE)

    async def test_set_downloading_progress_updates_step(self):
        """set_downloading_progress stores percentage in step detail."""
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=1, reply_to_message_id=10)
        await pm.create()
        pm._last_edit_time = 0.0

        await pm.set_downloading_progress(55.0)
        # State should reflect percentage (55.0 rounds to "55%")
        _status, detail = pm._steps[Step.DOWNLOADING]
        assert "55" in detail

    async def test_set_playlist_context_updates_render(self):
        """set_playlist_context stores track/total and render reflects them."""
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=1, reply_to_message_id=10)
        await pm.create()
        pm._last_edit_time = 0.0

        await pm.set_playlist_context(track_index=2, total_tracks=8)
        assert pm._playlist_track == 2
        assert pm._playlist_total == 8
        text = pm.render()
        assert "2" in text and "8" in text

    async def test_set_playlist_context_stores_title(self):
        """set_playlist_context with track_title stores and renders the title."""
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=1, reply_to_message_id=10)
        await pm.create()
        pm._last_edit_time = 0.0

        await pm.set_playlist_context(
            track_index=3, total_tracks=10, track_title="Song Title"
        )
        assert pm._playlist_track_title == "Song Title"
        text = pm.render()
        assert "Song Title" in text

    async def test_set_playlist_context_backward_compat_no_title(self):
        """set_playlist_context without track_title leaves title as None."""
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=1, reply_to_message_id=10)
        await pm.create()
        pm._last_edit_time = 0.0

        await pm.set_playlist_context(track_index=1, total_tracks=5)
        assert pm._playlist_track_title is None


class TestUploadAnimation:
    async def test_start_upload_animation_creates_task(self):
        """start_upload_animation() creates a background task."""
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=1, reply_to_message_id=10)
        pm._message_id = 1
        await pm.start_upload_animation()
        assert Step.UPLOADING in pm._animation_tasks
        await pm.stop_upload_animation()

    async def test_stop_upload_animation_cancels_task(self):
        """stop_upload_animation() cancels and clears the task."""
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=1, reply_to_message_id=10)
        pm._message_id = 1
        await pm.start_upload_animation()
        await pm.stop_upload_animation()
        assert Step.UPLOADING not in pm._animation_tasks

    async def test_start_animation_noop_if_already_running(self):
        """start_upload_animation() is idempotent — calling twice keeps one task."""
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=1, reply_to_message_id=10)
        pm._message_id = 1
        await pm.start_upload_animation()
        first_task = pm._animation_tasks[Step.UPLOADING]
        await pm.start_upload_animation()
        assert pm._animation_tasks[Step.UPLOADING] is first_task
        await pm.stop_upload_animation()

    async def test_set_step_uploading_done_stops_animation(self):
        """set_step(UPLOADING, DONE) auto-stops the upload animation."""
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=1, reply_to_message_id=10)
        pm._message_id = 1
        pm._last_edit_time = 0.0
        await pm.start_upload_animation()
        await pm.set_step(Step.UPLOADING, StepStatus.DONE)
        assert Step.UPLOADING not in pm._animation_tasks

    async def test_set_step_uploading_error_stops_animation(self):
        """set_step(UPLOADING, ERROR) auto-stops the upload animation."""
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=1, reply_to_message_id=10)
        pm._message_id = 1
        pm._last_edit_time = 0.0
        await pm.start_upload_animation()
        await pm.set_step(Step.UPLOADING, StepStatus.ERROR)
        assert Step.UPLOADING not in pm._animation_tasks

    async def test_stop_animation_without_start_is_safe(self):
        """stop_upload_animation() when no animation is running does not raise."""
        bot = make_bot()
        pm = ProgressManager(bot, chat_id=1, reply_to_message_id=10)
        pm._message_id = 1
        # Should not raise
        await pm.stop_upload_animation()
