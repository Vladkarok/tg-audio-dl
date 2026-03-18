"""Main Telegram bot handlers.

Bot data keys expected in context.bot_data:
    "settings"   — Settings instance
    "downloader" — AudioDownloader instance
    "cache"      — CacheBackend instance
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import io
import logging
import time
from pathlib import Path

import mutagen
from PIL import Image
from telegram import Bot, InputFile, Message, Update
from telegram.ext import ContextTypes

from src.bot.progress import ProgressManager, Step, StepStatus
from src.cache.base import CacheBackend
from src.config import Settings
from src.downloader.client import (
    AudioDownloader,
    Chapter,
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
_rate_limit_lock = asyncio.Lock()
_rate_limit_request_count: int = 0
_RATE_LIMIT_CLEANUP_INTERVAL: int = 100

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

    settings: Settings = context.bot_data["settings"]

    user_id: int = update.effective_user.id
    text: str = update.message.text or ""

    # --- Access control ---------------------------------------------------
    allowed: list[int] = settings.ALLOWED_USER_IDS
    if allowed and user_id not in allowed:
        logger.warning("Rejected user %d (not in ALLOWED_USER_IDS)", user_id)
        return

    # --- Parse URL --------------------------------------------------------
    parsed_urls = extract_media_urls(text)
    if not parsed_urls:
        return  # silently ignore non-YouTube messages

    parsed_url = parsed_urls[0]

    # --- Rate limiting ----------------------------------------------------
    async with _rate_limit_lock:
        global _rate_limit_request_count  # noqa: PLW0603
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

        # Periodic cleanup of stale entries to prevent unbounded growth
        _rate_limit_request_count += 1
        if _rate_limit_request_count >= _RATE_LIMIT_CLEANUP_INTERVAL:
            _rate_limit_request_count = 0
            stale_uids = [
                uid
                for uid, ts in _user_request_times.items()
                if not any(now - t < 60.0 for t in ts)
            ]
            for uid in stale_uids:
                del _user_request_times[uid]

    # --- Progress message -------------------------------------------------
    progress = ProgressManager(
        context.bot,
        chat_id=update.message.chat_id,
        reply_to_message_id=update.message.message_id,
    )
    await progress.create()

    try:
        await _process_url(update, context, progress, parsed_url)
    except Exception:
        logger.exception("Unexpected error in handle_url for user %d", user_id)
        with contextlib.suppress(Exception):
            await progress.set_step(
                Step.UPLOADING, StepStatus.ERROR, "An unexpected error occurred"
            )


async def _process_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    progress: ProgressManager,
    parsed_url: ParsedURL,
) -> None:
    """Inner orchestration: cache check → download → upload."""
    if update.message is None:
        return

    settings: Settings = context.bot_data["settings"]
    downloader: AudioDownloader = context.bot_data["downloader"]
    cache: CacheBackend = context.bot_data["cache"]
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
                # Build caption with chapters for file_id resend
                fid_chapters = await cache.get_chapters(video_id)
                fid_path = await cache.get(video_id)
                fid_title = video_id
                if fid_path:
                    t, _ = _extract_audio_metadata(fid_path)
                    if t:
                        fid_title = clean_title(t) or video_id
                fid_caption = _build_caption(fid_title, fid_chapters)
                await context.bot.send_audio(
                    chat_id=update.message.chat_id,
                    audio=file_id,
                    caption=fid_caption,
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
            # Cache metadata said "exists" but the file is gone (S3 error,
            # race eviction, etc.).  Fall through to a fresh download instead
            # of silently returning with a stale progress message.
            logger.warning("Cache exists() but get() returned None for %s", video_id)
        else:
            cached_title, cached_artist = _extract_audio_metadata(cached_path)
            cached_chapters = await cache.get_chapters(video_id)
            result = DownloadResult(
                file_path=cached_path,
                video_id=video_id,
                title=cached_title or video_id,
                artist=cached_artist,
                duration_seconds=None,
                thumbnail_url=None,
                file_size_bytes=cached_path.stat().st_size,
                chapters=cached_chapters,
            )
            msg = await _send_audio(
                context.bot, update.message.chat_id, result, progress
            )
            if msg.audio:
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

        # Store in cache — put() moves the file, so use the returned path
        stored_path: Path | None = None
        try:
            stored_path = await cache.put(result.video_id, result.file_path)
        except Exception:
            logger.exception("Cache put failed for video_id=%s", result.video_id)

        # Use cached path if available, otherwise fall back to original
        send_result = (
            dataclasses.replace(result, file_path=stored_path)
            if stored_path
            else result
        )
        msg = await _send_audio(
            context.bot, update.message.chat_id, send_result, progress
        )
        if msg.audio:
            with contextlib.suppress(Exception):
                await cache.store_file_id(result.video_id, msg.audio.file_id)
        if result.chapters:
            with contextlib.suppress(Exception):
                await cache.store_chapters(result.video_id, result.chapters)

    await progress.set_step(Step.UPLOADING, StepStatus.DONE)
    await asyncio.sleep(2)
    await progress.delete()


def _extract_audio_metadata(file_path: Path) -> tuple[str | None, str | None]:
    """Extract title and artist from audio tags. Returns (title, artist).

    Supports M4A/MP4, Opus/Vorbis (webm/ogg), and MP3 (ID3) via mutagen
    auto-detection.
    """
    try:
        audio = mutagen.File(file_path)  # type: ignore[attr-defined]
        if audio is None or audio.tags is None:
            return None, None
        tags = audio.tags
        # M4A / MP4
        if hasattr(tags, "get") and "\xa9nam" in tags:
            title = tags.get("\xa9nam", [None])[0]
            artist = tags.get("\xa9ART", [None])[0]
            return title, artist
        # Vorbis (Opus, OGG, WebM)
        if hasattr(tags, "get") and "title" in tags:
            title = tags.get("title", [None])[0]
            artist = tags.get("artist", [None])[0]
            return title, artist
        # ID3 (MP3)
        if hasattr(tags, "getall"):
            tit2 = tags.getall("TIT2")
            tpe1 = tags.getall("TPE1")
            title = str(tit2[0]) if tit2 else None
            artist = str(tpe1[0]) if tpe1 else None
            return title, artist
        return None, None
    except Exception:
        logger.debug("Could not read metadata from %s", file_path, exc_info=True)
        return None, None


# ---------------------------------------------------------------------------
# Caption formatting
# ---------------------------------------------------------------------------


def _format_timestamp(seconds: int) -> str:
    """Format seconds as HH:MM:SS."""
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _build_caption(
    title: str,
    chapters: tuple[Chapter, ...] | None,
    max_length: int = 1024,
) -> str:
    """Build a Telegram caption with title and optional chapter timestamps.

    Truncates chapter list from the bottom if it exceeds *max_length*,
    appending '...' as indicator.
    """
    title_line = f"🎵 {title}"
    if not chapters:
        return title_line[:max_length]

    chapter_lines = [f"{_format_timestamp(start)} {label}" for start, label in chapters]

    full = title_line + "\n\n" + "\n".join(chapter_lines)
    if len(full) <= max_length:
        return full

    # Truncate: drop chapters from the bottom, add "..." suffix
    suffix = "\n..."
    base = title_line + "\n\n"
    available = max_length - len(base) - len(suffix)

    if available <= 0:
        return title_line[:max_length]

    included: list[str] = []
    used = 0
    for line in chapter_lines:
        needed = len(line) + (1 if included else 0)  # +1 for \n separator
        if used + needed > available:
            break
        used += needed
        included.append(line)

    # Show at least 2 chapters or fall back to title only
    if len(included) < 2:
        return title_line[:max_length]

    return base + "\n".join(included) + suffix


_THUMB_MAX_SIZE = 320  # Telegram max thumbnail dimension (px)
_THUMB_QUALITY = 80  # JPEG quality for resized thumbnails


def _resize_thumbnail(raw: bytes) -> bytes:
    """Resize cover art to fit Telegram's 320x320 thumbnail limit.

    Returns JPEG bytes. If the image is already small enough, returns it as-is.
    """
    img = Image.open(io.BytesIO(raw))
    if img.width <= _THUMB_MAX_SIZE and img.height <= _THUMB_MAX_SIZE:
        return raw
    img.thumbnail((_THUMB_MAX_SIZE, _THUMB_MAX_SIZE), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=_THUMB_QUALITY)
    return buf.getvalue()


def _extract_thumbnail(file_path: Path) -> InputFile | None:
    """Extract embedded cover art from an audio file as an InputFile for Telegram.

    Supports M4A (covr tag), Opus/Vorbis (metadata_block_picture), and MP3 (APIC).
    Images are resized to max 320x320 to comply with Telegram's thumbnail limits.
    """
    try:
        audio = mutagen.File(file_path)  # type: ignore[attr-defined]
        if audio is None or audio.tags is None:
            return None
        tags = audio.tags
        raw: bytes | None = None
        # M4A / MP4: covr tag
        if hasattr(tags, "get") and "covr" in tags and tags["covr"]:
            raw = bytes(tags["covr"][0])
        # Vorbis (Opus/OGG): metadata_block_picture
        elif hasattr(tags, "get") and "metadata_block_picture" in tags:
            import base64

            from mutagen.flac import Picture

            pic_data = base64.b64decode(tags["metadata_block_picture"][0])
            picture = Picture(pic_data)  # type: ignore[no-untyped-call]
            raw = picture.data
        # ID3 (MP3): APIC frames
        elif hasattr(tags, "getall"):
            apic_frames = tags.getall("APIC")
            if apic_frames:
                raw = apic_frames[0].data

        if raw is None:
            return None
        resized = _resize_thumbnail(raw)
        return InputFile(io.BytesIO(resized), filename="cover.jpg")
    except Exception:
        logger.debug("Could not extract thumbnail from %s", file_path, exc_info=True)
    return None


async def _send_audio(
    bot: Bot, chat_id: int, result: DownloadResult, progress: ProgressManager
) -> Message:
    """Send a single audio file to the user. Returns the sent Message."""
    await progress.set_step(Step.PROCESSING, StepStatus.ACTIVE)
    await progress.start_animation(Step.PROCESSING)
    display_title = clean_title(result.title) or result.video_id
    safe_filename = sanitize_filename(display_title) or result.video_id
    thumbnail = _extract_thumbnail(result.file_path)
    await progress.set_step(Step.PROCESSING, StepStatus.DONE)
    await progress.start_upload_animation()
    with result.file_path.open("rb") as audio_file:
        msg = await bot.send_audio(
            chat_id=chat_id,
            audio=audio_file,
            thumbnail=thumbnail,
            title=display_title,
            performer=result.artist,
            duration=result.duration_seconds,
            caption=_build_caption(display_title, result.chapters),
            filename=f"{safe_filename}{result.file_path.suffix}",
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
