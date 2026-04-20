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
from dataclasses import dataclass, field
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
from src.downloader.url_parser import ParsedURL, URLType, extract_media_urls
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
_RATE_LIMIT_CLEANUP_INTERVAL: int = 20

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
    "• https://on.soundcloud.com/...\n\n"
    "Commands:\n"
    "• /refresh <URL> — re-check chapters/metadata and resend "
    "(useful when the author added timestamps after upload).\n"
    "• /redownload <URL> — evict the cached copy and download again "
    "from scratch.\n"
)


# ---------------------------------------------------------------------------
# Shared access-control + rate-limit helpers
# ---------------------------------------------------------------------------


def _is_user_allowed(user_id: int, settings: Settings) -> bool:
    """Return True if *user_id* is permitted (or the allow-list is empty)."""
    allowed = settings.ALLOWED_USER_IDS
    return not allowed or user_id in allowed


async def _consume_rate_limit(user_id: int, settings: Settings) -> bool:
    """Atomically check-and-record the per-user sliding-window rate limit.

    Returns True when the request fits within the 60-second window (a slot
    has been consumed); False when the user has exceeded their limit.
    """
    async with _rate_limit_lock:
        global _rate_limit_request_count  # noqa: PLW0603
        rate_limit: int = settings.RATE_LIMIT_PER_MINUTE
        now = time.monotonic()
        timestamps = _user_request_times.get(user_id, [])
        recent = [t for t in timestamps if now - t < 60.0]
        if len(recent) >= rate_limit:
            if recent:
                _user_request_times[user_id] = recent
            else:
                _user_request_times.pop(user_id, None)
            return False
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
    return True


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
    if not _is_user_allowed(user_id, settings):
        logger.warning("Rejected user %d (not in ALLOWED_USER_IDS)", user_id)
        return

    # --- Parse URL --------------------------------------------------------
    parsed_urls = extract_media_urls(text)
    if not parsed_urls:
        return  # silently ignore non-YouTube messages

    parsed_url = parsed_urls[0]

    # --- Rate limiting ----------------------------------------------------
    if not await _consume_rate_limit(user_id, settings):
        await update.message.reply_text(
            "You have reached the rate limit. Please wait a minute."
        )
        return

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


# ---------------------------------------------------------------------------
# /redownload command — evict cache then force a fresh download
# ---------------------------------------------------------------------------


_REDOWNLOAD_USAGE = (
    "Usage: /redownload <YouTube or SoundCloud URL>\n"
    "Evicts the cached copy (if any) and downloads again from scratch."
)


async def handle_redownload(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Force a full redownload, ignoring any existing cache entry.

    Useful when the upstream audio has changed or when sidecar metadata
    (chapters, file_id) needs a clean reset.
    """
    if update.effective_user is None or update.message is None:
        return

    settings: Settings = context.bot_data["settings"]
    user_id: int = update.effective_user.id
    text: str = update.message.text or ""

    if not _is_user_allowed(user_id, settings):
        logger.warning("Rejected user %d (not in ALLOWED_USER_IDS)", user_id)
        return

    parsed_urls = extract_media_urls(text)
    if not parsed_urls:
        await update.message.reply_text(_REDOWNLOAD_USAGE)
        return

    parsed_url = parsed_urls[0]

    if not await _consume_rate_limit(user_id, settings):
        await update.message.reply_text(
            "You have reached the rate limit. Please wait a minute."
        )
        return

    cache: CacheBackend = context.bot_data["cache"]

    # Evict cached audio + file_id + chapters sidecar before redownloading so
    # the fresh download lands on a clean slate. Skipped for playlists (they
    # have no top-level cache key; per-track caches are replaced on re-put).
    if parsed_url.video_id:
        try:
            await cache.evict(parsed_url.video_id)
        except Exception:
            # Non-fatal: a failing evict still lets the download overwrite the
            # audio, at the cost of possibly-stale sidecars. The user's intent
            # (fresh audio) is preserved; we don't want to block on a flaky S3.
            logger.warning(
                "Failed to evict cache for %s before redownload",
                parsed_url.video_id,
                exc_info=True,
            )

    progress = ProgressManager(
        context.bot,
        chat_id=update.message.chat_id,
        reply_to_message_id=update.message.message_id,
    )
    await progress.create()

    try:
        await _process_url(
            update, context, progress, parsed_url, force_redownload=True
        )
    except Exception:
        logger.exception(
            "Unexpected error in handle_redownload for user %d", user_id
        )
        with contextlib.suppress(Exception):
            await progress.set_step(
                Step.UPLOADING, StepStatus.ERROR, "An unexpected error occurred"
            )


async def _process_url(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    progress: ProgressManager,
    parsed_url: ParsedURL,
    force_redownload: bool = False,
) -> None:
    """Inner orchestration: cache check → download → upload.

    When force_redownload is True, the cache-hit short-circuit is skipped —
    download always runs. Callers that pass True are expected to have already
    evicted any cached entry so the fresh download cleanly replaces it.
    """
    if update.message is None:
        return

    settings: Settings = context.bot_data["settings"]
    downloader: AudioDownloader = context.bot_data["downloader"]
    cache: CacheBackend = context.bot_data["cache"]
    video_id = parsed_url.video_id  # None for playlists

    # --- Cache check (single videos only) --------------------------------
    if not force_redownload and video_id and await cache.exists(video_id):
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
                fid_caption_result = _build_caption_result(fid_title, fid_chapters)
                fid_msg = await context.bot.send_audio(
                    chat_id=update.message.chat_id,
                    audio=file_id,
                    caption=fid_caption_result.caption,
                    read_timeout=300,
                    write_timeout=300,
                )
                if fid_caption_result.index_messages:
                    await _send_chapter_index(
                        context.bot,
                        update.message.chat_id,
                        fid_msg.message_id,
                        fid_caption_result.index_messages,
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
                title=cached_title or "Unknown Title",
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
                try:
                    await cache.store_file_id(video_id, msg.audio.file_id)
                except Exception:
                    logger.warning(
                        "Failed to store file_id for %s — fast resend unavailable",
                        video_id,
                        exc_info=True,
                    )
            await asyncio.sleep(2)
            await progress.delete()
            return

    # --- Download ---------------------------------------------------------
    chat_id = update.message.chat_id  # captured once; update.message is non-None here

    try:
        is_playlist = parsed_url.url_type == URLType.PLAYLIST
        initial_detail = "Fetching playlist…" if is_playlist else ""
        await progress.set_step(Step.DOWNLOADING, StepStatus.ACTIVE, initial_detail)

        async def on_progress(dp: DownloadProgress) -> None:
            await progress.set_downloading_progress(dp.percentage)

        handled_ids: set[str] = set()

        async def on_track_start(idx: int, total: int, title: str) -> None:
            await progress.set_playlist_context(
                track_index=idx, total_tracks=total, track_title=title
            )
            await progress.set_step(Step.DOWNLOADING, StepStatus.ACTIVE)

        async def on_track_ready(result: DownloadResult) -> None:
            handled_ids.add(result.video_id)
            await progress.set_step(Step.UPLOADING, StepStatus.ACTIVE)
            await _cache_and_upload_one(
                bot=context.bot,
                chat_id=chat_id,
                result=result,
                progress=progress,
                cache=cache,
            )

        results = await downloader.download(
            parsed_url,
            progress_callback=on_progress,
            max_tracks=settings.PLAYLIST_MAX_TRACKS,
            track_start_callback=on_track_start if is_playlist else None,
            track_ready_callback=on_track_ready if is_playlist else None,
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

    # Upload any results not already streamed via on_track_ready
    for result in results:
        if result.video_id in handled_ids:
            continue
        await progress.set_step(Step.UPLOADING, StepStatus.ACTIVE)
        await _cache_and_upload_one(
            bot=context.bot,
            chat_id=chat_id,
            result=result,
            progress=progress,
            cache=cache,
        )

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

_CAPTION_MAX = 1024
_MESSAGE_MAX = 4096


@dataclass(frozen=True)
class CaptionResult:
    """Caption for send_audio plus optional chapter-index reply messages."""

    caption: str
    # Each element is one reply message (≤4096 chars). Empty = no follow-up needed.
    index_messages: tuple[str, ...] = field(default_factory=tuple)


def _format_timestamp(seconds: int) -> str:
    """Format seconds as HH:MM:SS."""
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _normalize_chapters(chapters: tuple[Chapter, ...]) -> tuple[Chapter, ...]:
    """Strip whitespace, drop empty names, deduplicate start times (first wins)."""
    seen: set[int] = set()
    result: list[Chapter] = []
    for start, name in chapters:
        clean = " ".join(name.split())
        if not clean or start in seen:
            continue
        seen.add(start)
        result.append((start, clean))
    return tuple(result)


def _build_index_messages(
    chapters: tuple[Chapter, ...],
    header: str,
    include_timestamps: bool = False,
    max_length: int = _MESSAGE_MAX,
) -> tuple[str, ...]:
    """Pack chapter index lines into one or more messages each ≤ max_length chars.

    When include_timestamps is True each line is: ``HH:MM:SS N - Name``
    Otherwise: ``N - Name``
    """
    if include_timestamps:
        lines = [
            f"{_format_timestamp(s)} {i} - {name}"
            for i, (s, name) in enumerate(chapters, 1)
        ]
    else:
        lines = [f"{i} - {name}" for i, (_, name) in enumerate(chapters, 1)]

    messages: list[str] = []
    current: list[str] = [header]
    current_len = len(header)

    for line in lines:
        needed = 1 + len(line)  # leading \n
        if current_len + needed > max_length and current:
            messages.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += needed

    if current:
        messages.append("\n".join(current))

    return tuple(messages)


def _build_caption_result(
    title: str,
    chapters: tuple[Chapter, ...] | None,
) -> CaptionResult:
    """Build caption + optional chapter-index follow-up messages.

    Four-tier strategy (timestamps are NEVER individually truncated):

    Tier 1  Full caption (title + full chapter names) fits in 1024 chars.
            → Caption only, no follow-up.

    Tier 2  Full names don't fit; title + numbered timestamps fit.
            → Caption: ``🎵 Title\\n\\nHH:MM:SS 1\\n...``
            → Follow-up: ``📋 Chapters:\\n1 - Name\\n...``

    Tier 3  Title + numbered timestamps don't fit; numbered timestamps alone fit.
            → Caption: ``HH:MM:SS 1\\n...`` (no title)
            → Follow-up includes title header so user can still see it.

    Tier 4  Even bare numbered timestamps exceed 1024 (200+ chapters).
            → Caption: ``🎵 Title`` only (no timestamps at all).
            → Follow-up: ``🎵 Title\\n\\n📋 All chapters:\\nHH:MM:SS 1 - Name\\n...``
              so all navigation info is available in the reply.
    """
    title_line = f"🎵 {title}"

    if not chapters:
        return CaptionResult(caption=title_line[:_CAPTION_MAX])

    chapters = _normalize_chapters(chapters)
    if not chapters:
        return CaptionResult(caption=title_line[:_CAPTION_MAX])

    # --- Tier 1: full caption with chapter names ---
    full_lines = [f"{_format_timestamp(s)} {name}" for s, name in chapters]
    full_caption = title_line + "\n\n" + "\n".join(full_lines)
    if len(full_caption) <= _CAPTION_MAX:
        return CaptionResult(caption=full_caption)

    # --- Tier 2: numbered timestamps + title ---
    numbered_lines = [
        f"{_format_timestamp(s)} {i}" for i, (s, _) in enumerate(chapters, 1)
    ]
    tier2_caption = title_line + "\n\n" + "\n".join(numbered_lines)
    if len(tier2_caption) <= _CAPTION_MAX:
        index = _build_index_messages(chapters, header="📋 Chapters:")
        return CaptionResult(caption=tier2_caption, index_messages=index)

    # --- Tier 3: numbered timestamps only (no title) ---
    tier3_caption = "\n".join(numbered_lines)
    if len(tier3_caption) <= _CAPTION_MAX:
        header = f"🎵 {title}\n\n📋 Chapters:"
        index = _build_index_messages(chapters, header=header)
        return CaptionResult(caption=tier3_caption, index_messages=index)

    # --- Tier 4: title-only caption; all info in follow-up (with timestamps) ---
    header = f"🎵 {title}\n\n📋 All chapters:"
    index = _build_index_messages(chapters, header=header, include_timestamps=True)
    return CaptionResult(caption=title_line[:_CAPTION_MAX], index_messages=index)


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


async def _send_chapter_index(
    bot: Bot,
    chat_id: int,
    reply_to_message_id: int,
    index_messages: tuple[str, ...],
) -> None:
    """Send chapter index as reply message(s). Failures are logged and swallowed."""
    for text in index_messages:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_to_message_id=reply_to_message_id,
            )
        except Exception:
            logger.warning(
                "Failed to send chapter index chunk to chat %d — continuing",
                chat_id,
                exc_info=True,
            )


async def _cache_and_upload_one(
    bot: Bot,
    chat_id: int,
    result: DownloadResult,
    progress: ProgressManager,
    cache: CacheBackend,
) -> None:
    """Cache a DownloadResult then upload it to Telegram."""
    stored_path: Path | None = None
    try:
        stored_path = await cache.put(result.video_id, result.file_path)
    except Exception:
        logger.exception("Cache put failed for video_id=%s", result.video_id)

    send_result = (
        dataclasses.replace(result, file_path=stored_path) if stored_path else result
    )
    msg = await _send_audio(bot, chat_id, send_result, progress)
    if msg.audio:
        try:
            await cache.store_file_id(result.video_id, msg.audio.file_id)
        except Exception:
            logger.warning(
                "Failed to store file_id for %s — fast resend unavailable",
                result.video_id,
                exc_info=True,
            )
    if result.chapters:
        try:
            await cache.store_chapters(result.video_id, result.chapters)
        except Exception:
            logger.warning(
                "Failed to store chapters for %s", result.video_id, exc_info=True
            )


async def _send_audio(
    bot: Bot, chat_id: int, result: DownloadResult, progress: ProgressManager
) -> Message:
    """Send a single audio file to the user. Returns the sent Message."""
    await progress.set_step(Step.PROCESSING, StepStatus.ACTIVE)
    await progress.start_animation(Step.PROCESSING)
    display_title = clean_title(result.title) or result.video_id
    safe_filename = sanitize_filename(display_title) or result.video_id
    thumbnail = _extract_thumbnail(result.file_path)
    caption_result = _build_caption_result(display_title, result.chapters)
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
            caption=caption_result.caption,
            filename=f"{safe_filename}{result.file_path.suffix}",
            read_timeout=300,
            write_timeout=300,
        )
    if caption_result.index_messages:
        await _send_chapter_index(
            bot, chat_id, msg.message_id, caption_result.index_messages
        )
    return msg


async def _edit_error(bot: Bot, progress: ProgressManager, text: str) -> None:
    """Force-update the progress message with an error summary."""
    try:
        await progress.edit_text(text)
    except Exception:
        logger.exception("Failed to edit error message")
