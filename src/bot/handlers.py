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
import logging
import time
from pathlib import Path

from telegram import Bot, Message, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from src.bot.captions import (
    _CAPTION_MAX as _CAPTION_MAX,  # re-exported for tests
)
from src.bot.captions import (
    _build_caption_result,
    _build_chapter_pages,
    _build_chapter_pages_markup,
    _extract_chapter_page_title,
    _normalize_chapters,
    _parse_chapter_page_callback_data,
)
from src.bot.captions import (
    _format_timestamp as _format_timestamp,  # re-exported for tests
)
from src.bot.media import _extract_audio_metadata, _extract_thumbnail
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
_chapter_page_edit_locks: dict[tuple[int, int], asyncio.Lock] = {}

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
    "• /chapters <URL> — experimental paginated chapter captions "
    "(when enabled).\n"
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


async def handle_redownload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        await _process_url(update, context, progress, parsed_url, force_redownload=True)
    except Exception:
        logger.exception("Unexpected error in handle_redownload for user %d", user_id)
        with contextlib.suppress(Exception):
            await progress.set_step(
                Step.UPLOADING, StepStatus.ERROR, "An unexpected error occurred"
            )


# ---------------------------------------------------------------------------
# /refresh command — re-fetch chapters/metadata for a cached track, resend
# ---------------------------------------------------------------------------


_REFRESH_USAGE = (
    "Usage: /refresh <YouTube or SoundCloud URL>\n"
    "Re-checks chapters/metadata for a cached track and resends it.\n"
    "Cache miss falls through to a full download."
)


async def handle_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Refresh cached metadata (chapters) and resend the audio.

    - Cache miss: falls through to a normal download (same as handle_url).
    - Cache hit: fetch_metadata only, update chapters if they changed, then
      resend via file_id (fallback: re-upload from cache) with the fresh caption.
    - Always resends on the hit path — the user asked for a refresh, so a
      silent "nothing changed" would be confusing.
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
        await update.message.reply_text(_REFRESH_USAGE)
        return

    parsed_url = parsed_urls[0]

    # Refresh is a per-video operation; playlist-level metadata has no
    # cached chapters to refresh.
    if parsed_url.url_type != URLType.SINGLE:
        await update.message.reply_text(
            "/refresh supports single videos only (not playlists or mixes)."
        )
        return

    if not await _consume_rate_limit(user_id, settings):
        await update.message.reply_text(
            "You have reached the rate limit. Please wait a minute."
        )
        return

    cache: CacheBackend = context.bot_data["cache"]
    downloader: AudioDownloader = context.bot_data["downloader"]
    video_id = parsed_url.video_id

    progress = ProgressManager(
        context.bot,
        chat_id=update.message.chat_id,
        reply_to_message_id=update.message.message_id,
    )
    await progress.create()

    try:
        # --- Cache miss → full download (no silent no-op) ----------------
        # parsed_url.video_id is None for on.soundcloud.com short URLs; we
        # can't key the cache without resolving the slug first. Falling
        # through to a full download is suboptimal (it re-downloads audio
        # that may already be cached under the resolved sc_slug) but still
        # correct — the user gets the track they asked to refresh. Users who
        # want the cheap metadata-only path should paste the canonical URL.
        if not video_id or not await cache.exists(video_id):
            await _process_url(update, context, progress, parsed_url)
            return

        # --- Cache hit → metadata-only refresh ---------------------------
        # The audio file is already cached; only upstream metadata is fetched.
        # Keep this out of the DOWNLOADING step so the progress UI does not
        # imply that the audio is being downloaded again.
        await progress.set_step(Step.DOWNLOADING, StepStatus.DONE, "Found in cache")
        await progress.set_step(
            Step.PROCESSING, StepStatus.ACTIVE, "Fetching metadata…"
        )
        try:
            metadata = await downloader.fetch_metadata(parsed_url)
        except (VideoUnavailableError, DownloadError) as exc:
            logger.info("Refresh metadata fetch failed for %s", video_id, exc_info=True)
            await progress.set_step(Step.PROCESSING, StepStatus.ERROR)
            await _edit_error(context.bot, progress, f"❌ {_user_facing_error(exc)}")
            return

        old_chapters = await cache.get_chapters(video_id)
        old_norm = _normalize_chapters(old_chapters or ())

        if metadata.chapters is None:
            # Transient extractor miss — keep good data.
            final_chapters = old_chapters
        else:
            new_norm = _normalize_chapters(metadata.chapters)
            if new_norm != old_norm:
                try:
                    await cache.store_chapters(video_id, metadata.chapters)
                except Exception:
                    logger.warning(
                        "Failed to store refreshed chapters for %s",
                        video_id,
                        exc_info=True,
                    )
                final_chapters = metadata.chapters
            else:
                final_chapters = old_chapters

        await progress.set_step(Step.PROCESSING, StepStatus.DONE)

        display_title = clean_title(metadata.title) or video_id
        caption_result = _build_caption_result(display_title, final_chapters)

        # --- Fast path: resend via Telegram file_id -----------------------
        file_id = await cache.get_file_id(video_id)
        sent_msg: Message | None = None
        if file_id:
            try:
                await progress.set_step(Step.UPLOADING, StepStatus.ACTIVE)
                await progress.start_upload_animation()
                sent_msg = await context.bot.send_audio(
                    chat_id=update.message.chat_id,
                    audio=file_id,
                    caption=caption_result.caption,
                    read_timeout=300,
                    write_timeout=300,
                )
            except Exception:
                logger.warning(
                    "file_id resend failed for %s, falling back to upload",
                    video_id,
                )
                sent_msg = None

        # --- Fallback: re-upload from cached file -------------------------
        if sent_msg is None:
            cached_path: Path | None = await cache.get(video_id)
            if cached_path is None:
                # Stale cache: exists() said yes but the file is gone
                # (S3 eviction, race, etc.). Recover by downloading fresh
                # instead of surfacing an error — matches _process_url's
                # behavior in the same state.
                logger.warning(
                    "Refresh: cache.exists()=True but cache.get()=None for %s — "
                    "falling through to fresh download",
                    video_id,
                )
                # Reset any in-flight upload animation/state from the failed
                # file_id attempt so _process_url starts with a clean slate
                # (otherwise the UI shows upload-active while download runs).
                await progress.stop_upload_animation()
                await progress.set_step(Step.PROCESSING, StepStatus.PENDING)
                await progress.set_step(Step.UPLOADING, StepStatus.PENDING)
                await _process_url(
                    update, context, progress, parsed_url, force_redownload=True
                )
                return
            cached_title, cached_artist = _extract_audio_metadata(cached_path)
            result = DownloadResult(
                file_path=cached_path,
                video_id=video_id,
                title=display_title or cached_title or video_id,
                artist=cached_artist,
                duration_seconds=None,
                thumbnail_url=None,
                file_size_bytes=cached_path.stat().st_size,
                chapters=final_chapters,
            )
            sent_msg = await _send_audio(
                context.bot, update.message.chat_id, result, progress
            )
            if sent_msg.audio:
                try:
                    await cache.store_file_id(video_id, sent_msg.audio.file_id)
                except Exception:
                    logger.warning(
                        "Failed to store refreshed file_id for %s",
                        video_id,
                        exc_info=True,
                    )
        else:
            # file_id path succeeded — chapter index reply still needed if overflow
            if caption_result.index_messages:
                await _send_chapter_index(
                    context.bot,
                    update.message.chat_id,
                    sent_msg.message_id,
                    caption_result.index_messages,
                )

        await progress.set_step(Step.UPLOADING, StepStatus.DONE)
        await asyncio.sleep(2)
        await progress.delete()
    except Exception:
        logger.exception("Unexpected error in handle_refresh for user %d", user_id)
        with contextlib.suppress(Exception):
            await progress.set_step(
                Step.UPLOADING, StepStatus.ERROR, "An unexpected error occurred"
            )


_CHAPTERS_USAGE = (
    "Usage: /chapters <YouTube or SoundCloud URL>\n"
    "Experimental paginated chapter captions. Works only for cached single tracks."
)


async def handle_chapters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send cached audio with experimental paginated chapter captions.

    This is intentionally cache-only: the command may fetch metadata, but it
    never downloads audio from the source. Normal URL handling remains the
    stable path.
    """
    if update.effective_user is None or update.message is None:
        return

    settings: Settings = context.bot_data["settings"]
    if not settings.EXPERIMENTAL_CHAPTER_PAGES_ENABLED:
        await update.message.reply_text("Experimental chapter pages are disabled.")
        return

    user_id = update.effective_user.id
    if not _is_user_allowed(user_id, settings):
        logger.warning("Rejected user %d (not in ALLOWED_USER_IDS)", user_id)
        return

    parsed_urls = extract_media_urls(update.message.text or "")
    if not parsed_urls:
        await update.message.reply_text(_CHAPTERS_USAGE)
        return

    parsed_url = parsed_urls[0]
    if parsed_url.url_type != URLType.SINGLE or not parsed_url.video_id:
        await update.message.reply_text("/chapters supports cached single tracks only.")
        return

    if not await _consume_rate_limit(user_id, settings):
        await update.message.reply_text(
            "You have reached the rate limit. Please wait a minute."
        )
        return

    cache: CacheBackend = context.bot_data["cache"]
    downloader: AudioDownloader = context.bot_data["downloader"]
    video_id = parsed_url.video_id

    if not await cache.exists(video_id):
        await update.message.reply_text(
            "This track is not cached yet. Send the URL normally first."
        )
        return

    chapters = await cache.get_chapters(video_id)
    display_title = video_id
    metadata_checked = False
    if not chapters:
        status = await update.message.reply_text("Fetching chapter metadata...")
        try:
            metadata = await downloader.fetch_metadata(parsed_url)
        except (VideoUnavailableError, DownloadError) as exc:
            await status.edit_text(f"❌ {_user_facing_error(exc)}")
            return
        metadata_checked = True
        display_title = _usable_track_title(metadata.title, video_id) or video_id
        chapters = metadata.chapters
        if chapters:
            try:
                await cache.store_chapters(video_id, chapters)
            except Exception:
                logger.warning(
                    "Failed to store chapters for %s from /chapters",
                    video_id,
                    exc_info=True,
                )
        with contextlib.suppress(Exception):
            await status.delete()

    if not chapters:
        await update.message.reply_text("No chapters found for this track.")
        return

    cached_path = await cache.get(video_id)
    cached_artist = None
    if cached_path is not None:
        cached_title, cached_artist = _extract_audio_metadata(cached_path)
        cached_display_title = _usable_track_title(cached_title, video_id)
        if display_title == video_id and cached_display_title:
            display_title = cached_display_title

    if display_title == video_id and not metadata_checked:
        try:
            metadata = await downloader.fetch_metadata(parsed_url)
        except (VideoUnavailableError, DownloadError):
            logger.info(
                "Could not fetch display title for cached chapters %s",
                video_id,
                exc_info=True,
            )
        else:
            display_title = _usable_track_title(metadata.title, video_id) or video_id

    pages = _build_chapter_pages(display_title, chapters)
    if not pages:
        await update.message.reply_text(
            "Chapter titles are too long for paginated captions."
        )
        return

    markup = _build_chapter_pages_markup(video_id, pages)
    if len(pages) > 1 and markup is None:
        await update.message.reply_text(
            "This cache key is too long for Telegram callback buttons."
        )
        return

    file_id = await cache.get_file_id(video_id)
    if file_id:
        await context.bot.send_audio(
            chat_id=update.message.chat_id,
            audio=file_id,
            caption=pages[0].caption,
            reply_markup=markup,
            read_timeout=300,
            write_timeout=300,
        )
        return

    if cached_path is None:
        await update.message.reply_text("Cached audio file is missing.")
        return

    safe_filename = sanitize_filename(display_title) or video_id
    thumbnail = _extract_thumbnail(cached_path)
    with cached_path.open("rb") as audio_file:
        msg = await context.bot.send_audio(
            chat_id=update.message.chat_id,
            audio=audio_file,
            thumbnail=thumbnail,
            title=display_title,
            performer=cached_artist,
            caption=pages[0].caption,
            reply_markup=markup,
            filename=f"{safe_filename}{cached_path.suffix}",
            read_timeout=300,
            write_timeout=300,
        )
    if msg.audio:
        try:
            await cache.store_file_id(video_id, msg.audio.file_id)
        except Exception:
            logger.warning(
                "Failed to store file_id for %s from /chapters",
                video_id,
                exc_info=True,
            )


async def handle_chapter_page_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle inline buttons for experimental chapter caption pages."""
    query = update.callback_query
    if query is None:
        return

    settings: Settings = context.bot_data["settings"]
    if not settings.EXPERIMENTAL_CHAPTER_PAGES_ENABLED:
        await query.answer("Experimental chapter pages are disabled.", show_alert=True)
        return

    if update.effective_user is not None and not _is_user_allowed(
        update.effective_user.id, settings
    ):
        await query.answer("Not allowed.", show_alert=True)
        return

    parsed = _parse_chapter_page_callback_data(query.data or "")
    if parsed is None:
        await query.answer("Invalid chapter page.", show_alert=True)
        return
    video_id, page_index = parsed

    message_key = _chapter_page_message_key(query.message)
    edit_lock = None
    if message_key is not None:
        edit_lock = _chapter_page_edit_locks.setdefault(message_key, asyncio.Lock())
        if edit_lock.locked():
            await query.answer()
            return
        await edit_lock.acquire()

    try:
        await query.answer()

        cache: CacheBackend = context.bot_data["cache"]
        chapters = await cache.get_chapters(video_id)
        title = (
            _extract_chapter_page_title(getattr(query.message, "caption", None))
            or video_id
        )
        pages = _build_chapter_pages(title, chapters) if chapters else ()
        if not pages or page_index >= len(pages):
            return

        markup = _build_chapter_pages_markup(video_id, pages)
        if len(pages) > 1 and markup is None:
            return

        try:
            await query.edit_message_caption(
                caption=pages[page_index].caption,
                reply_markup=markup,
            )
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise
    finally:
        if edit_lock is not None and message_key is not None:
            edit_lock.release()
            if _chapter_page_edit_locks.get(message_key) is edit_lock:
                _chapter_page_edit_locks.pop(message_key, None)


def _chapter_page_message_key(message: object | None) -> tuple[int, int] | None:
    if message is None:
        return None
    chat_id = getattr(message, "chat_id", None)
    if not isinstance(chat_id, int):
        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
    message_id = getattr(message, "message_id", None)
    if not isinstance(chat_id, int) or not isinstance(message_id, int):
        return None
    return chat_id, message_id


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


def _usable_track_title(title: str | None, video_id: str) -> str | None:
    """Return a display title, rejecting IDs and source URLs stored in tags."""
    cleaned = clean_title(title or "")
    if not cleaned or cleaned == video_id:
        return None
    lowered = cleaned.lower()
    if lowered.startswith(("http://", "https://", "www.")):
        return None
    if "youtube.com/" in lowered or "youtu.be/" in lowered:
        return None
    return cleaned


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
