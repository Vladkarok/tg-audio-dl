"""Main Telegram bot handlers.

Bot data keys expected in context.bot_data:
    "settings"   — Settings instance
    "downloader" — AudioDownloader instance
    "cache"      — CacheBackend instance
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

from telegram import Bot, Message, Update
from telegram.ext import ContextTypes

from src.bot.progress import ProgressManager, Step, StepStatus
from src.cache.base import CacheBackend
from src.config import Settings
from src.downloader.client import (
    AudioDownloader,
    DownloadError,
    DownloadProgress,
    DownloadResult,
    FileTooLargeError,
    VideoUnavailableError,
)
from src.downloader.url_parser import ParsedURL, extract_media_urls
from src.utils.sanitize import clean_title, sanitize_filename

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Safe user-facing error messages
# ---------------------------------------------------------------------------


def _user_facing_error(exc: Exception) -> str:
    """Return a safe user-facing error message.

    Never leaks internal paths or yt-dlp internals.
    """
    if isinstance(exc, FileTooLargeError):
        mb = exc.file_size_bytes / (1024 * 1024)
        max_mb = exc.max_bytes // (1024 * 1024)
        return f"File too large ({mb:.0f} MB). Maximum allowed size is {max_mb} MB."
    if isinstance(exc, VideoUnavailableError):
        return "The video is unavailable, private, age-restricted, or geo-blocked."
    if isinstance(exc, DownloadError):
        return "Download failed. The video may be unavailable or unsupported."
    return "An unexpected error occurred. Please try again."


# ---------------------------------------------------------------------------
# In-memory rate-limit store: user_id → list of request timestamps
# ---------------------------------------------------------------------------

_user_request_times: dict[int, list[float]] = {}

_WELCOME_TEXT = (
    "👋 Welcome! Send me a YouTube video or playlist URL and I will download "
    "the audio and send it back to you as an audio file."
)

_HELP_TEXT = (
    "Send a YouTube or SoundCloud link and I'll download the audio.\n\n"
    "YouTube:\n"
    "• https://www.youtube.com/watch?v=...\n"
    "• https://youtu.be/...\n"
    "• https://www.youtube.com/shorts/...\n"
    "• https://www.youtube.com/playlist?list=...\n\n"
    "SoundCloud:\n"
    "• https://soundcloud.com/artist/track\n"
    "• https://soundcloud.com/artist/sets/playlist\n"
    "• https://on.soundcloud.com/...\n"
)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with welcome message explaining bot usage."""
    if update.message is None:
        return
    await update.message.reply_text(_WELCOME_TEXT)


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with help text."""
    if update.message is None:
        return
    await update.message.reply_text(_HELP_TEXT)


# ---------------------------------------------------------------------------
# URL handler
# ---------------------------------------------------------------------------


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main handler: parse URL → check cache → download → upload."""
    if update.effective_user is None or update.message is None:
        return

    settings = context.bot_data["settings"]
    downloader = context.bot_data["downloader"]
    cache = context.bot_data["cache"]

    user_id: int = update.effective_user.id
    text: str = update.message.text or ""

    # --- Access control ---------------------------------------------------
    allowed: list[int] = settings.ALLOWED_USER_IDS
    if allowed and user_id not in allowed:
        logger.debug("Rejected user %d (not in ALLOWED_USER_IDS)", user_id)
        return

    # --- Parse URL --------------------------------------------------------
    parsed_urls = extract_media_urls(text)
    if not parsed_urls:
        return  # silently ignore non-YouTube messages

    parsed_url = parsed_urls[0]

    # --- Rate limiting ----------------------------------------------------
    rate_limit: int = settings.RATE_LIMIT_PER_MINUTE
    now = time.monotonic()
    timestamps = _user_request_times.get(user_id, [])
    # Keep only timestamps within the last 60 seconds
    recent = [t for t in timestamps if now - t < 60.0]
    if len(recent) >= rate_limit:
        if recent:
            _user_request_times[user_id] = recent
        else:
            _user_request_times.pop(user_id, None)
        await update.message.reply_text(
            "You have reached the rate limit. Please wait a minute."
        )
        return
    recent.append(now)
    _user_request_times[user_id] = recent

    # --- Progress message -------------------------------------------------
    progress = ProgressManager(
        context.bot,
        chat_id=update.message.chat_id,
        reply_to_message_id=update.message.message_id,
    )
    await progress.create()

    try:
        await _process_url(
            update, context, progress, parsed_url, downloader, cache, settings
        )
    except Exception:
        logger.exception("Unexpected error in handle_url for user %d", user_id)
        with contextlib.suppress(Exception):
            await progress.set_step(
                Step.UPLOADING, StepStatus.ERROR, "An unexpected error occurred"
            )


async def _process_url(  # noqa: PLR0913
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    progress: ProgressManager,
    parsed_url: ParsedURL,
    downloader: AudioDownloader,
    cache: CacheBackend,
    settings: Settings,
) -> None:
    """Inner orchestration: cache check → download → upload."""
    assert update.message is not None  # guaranteed by handle_url guard
    video_id = parsed_url.video_id  # None for playlists

    # --- Cache check (single videos only) --------------------------------
    if video_id and await cache.exists(video_id):
        await progress.set_step(Step.DOWNLOADING, StepStatus.DONE, "Found in cache")

        # Try instant resend via Telegram file_id if available
        file_id = await cache.get_file_id(video_id)
        if file_id:
            try:
                await progress.set_step(Step.PROCESSING, StepStatus.DONE)
                await progress.set_step(Step.UPLOADING, StepStatus.ACTIVE)
                await progress.start_upload_animation()
                await context.bot.send_audio(
                    chat_id=update.message.chat_id,
                    audio=file_id,
                    read_timeout=300,
                    write_timeout=300,
                )
                await progress.set_step(Step.UPLOADING, StepStatus.DONE)
                await asyncio.sleep(2)
                await progress.delete()
                return
            except Exception:
                logger.warning(
                    "file_id resend failed for %s, falling back to upload", video_id
                )
                # fall through to normal upload below

        # Cache hit but no file_id (or file_id failed) — upload file
        cached_path: Path | None = await cache.get(video_id)
        if cached_path is None:
            return
        result = DownloadResult(
            file_path=cached_path,
            video_id=video_id,
            title=video_id,
            artist=None,
            duration_seconds=None,
            thumbnail_url=None,
            file_size_bytes=cached_path.stat().st_size,
        )
        msg = await _send_audio(context.bot, update.message.chat_id, result, progress)
        if msg is not None and hasattr(msg, "audio") and msg.audio:
            with contextlib.suppress(Exception):
                await cache.store_file_id(video_id, msg.audio.file_id)
        await asyncio.sleep(2)
        await progress.delete()
        return

    # --- Download ---------------------------------------------------------
    try:
        await progress.set_step(Step.DOWNLOADING, StepStatus.ACTIVE)

        async def on_progress(dp: DownloadProgress) -> None:
            await progress.set_downloading_progress(dp.percentage)

        results = await downloader.download(
            parsed_url,
            progress_callback=on_progress,
            max_tracks=settings.PLAYLIST_MAX_TRACKS,
        )
    except FileTooLargeError as exc:
        logger.exception("File too large for user download")
        await progress.set_step(Step.UPLOADING, StepStatus.ERROR, "File too large")
        await _edit_error(
            context.bot,
            progress,
            f"❌ {_user_facing_error(exc)}",
        )
        return
    except DownloadError as exc:
        logger.exception("Download error")
        await progress.set_step(Step.DOWNLOADING, StepStatus.ERROR)
        await _edit_error(
            context.bot,
            progress,
            f"❌ {_user_facing_error(exc)}",
        )
        return

    # --- Cache store + Upload -------------------------------------------
    await progress.set_step(Step.DOWNLOADING, StepStatus.DONE)

    total = len(results)
    for idx, result in enumerate(results, start=1):
        if total > 1:
            await progress.set_playlist_context(track_index=idx, total_tracks=total)
        await progress.set_step(Step.UPLOADING, StepStatus.ACTIVE)

        # Store in cache (fire-and-forget errors are logged but not fatal)
        try:
            await cache.put(result.video_id, result.file_path)
        except Exception:
            logger.exception("Cache put failed for video_id=%s", result.video_id)

        msg = await _send_audio(context.bot, update.message.chat_id, result, progress)
        if msg is not None and hasattr(msg, "audio") and msg.audio:
            with contextlib.suppress(Exception):
                await cache.store_file_id(result.video_id, msg.audio.file_id)
        result.file_path.unlink(missing_ok=True)

    await progress.set_step(Step.UPLOADING, StepStatus.DONE)
    await asyncio.sleep(2)
    await progress.delete()


async def _send_audio(
    bot: Bot, chat_id: int, result: DownloadResult, progress: ProgressManager
) -> Message:
    """Send a single audio file to the user. Returns the sent Message."""
    await progress.set_step(Step.PROCESSING, StepStatus.DONE)
    display_title = clean_title(result.title) or result.video_id
    safe_filename = sanitize_filename(display_title) or result.video_id
    await progress.start_upload_animation()
    with result.file_path.open("rb") as audio_file:
        msg = await bot.send_audio(
            chat_id=chat_id,
            audio=audio_file,
            title=display_title,
            performer=result.artist,
            duration=result.duration_seconds,
            caption=f"🎵 {display_title}",
            filename=f"{safe_filename}.m4a",
            read_timeout=300,
            write_timeout=300,
        )
    return msg


async def _edit_error(bot: Bot, progress: ProgressManager, text: str) -> None:
    """Force-update the progress message with an error summary."""
    try:
        await progress.edit_text(text)
    except Exception:
        logger.exception("Failed to edit error message")
