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
from collections.abc import AsyncIterator
from pathlib import Path

from telegram import Bot, Message, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from src.bot.captions import (
    _CAPTION_MAX as _CAPTION_MAX,  # re-exported for tests
)
from src.bot.captions import (
    _build_audio_message,
    _build_chapter_pages,
    _build_chapter_pages_markup,
    _extract_chapter_page_title,
    _normalize_chapters,
    _parse_chapter_page_callback_data,
)
from src.bot.captions import (
    _build_caption_result as _build_caption_result,  # re-exported for tests
)
from src.bot.captions import (
    _format_timestamp as _format_timestamp,  # re-exported for tests
)
from src.bot.media import _extract_audio_metadata, _extract_thumbnail
from src.bot.progress import ProgressManager, Step, StepStatus
from src.bot.timestamps import TimestampParseError, parse_timestamps
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

# Per-media download locks (lock, refcount). Serialize the whole cache-check →
# download → cache-put sequence for one video so concurrent requests for the
# same uncached URL don't each trigger a redundant download — the second waits,
# then sees the entry the first just cached. Keyed by cache video_id; playlists
# (no single video_id) are not deduplicated here. Dict mutations are synchronous
# (no await between them) so no guard lock is needed.
_media_download_locks: dict[str, tuple[asyncio.Lock, int]] = {}


@contextlib.asynccontextmanager
async def _media_download_lock(video_id: str) -> AsyncIterator[None]:
    """Acquire the per-media download lock, refcounting so it's cleaned up."""
    entry = _media_download_locks.get(video_id)
    if entry is None:
        lock = asyncio.Lock()
        _media_download_locks[video_id] = (lock, 1)
    else:
        lock, count = entry
        _media_download_locks[video_id] = (lock, count + 1)
    try:
        async with lock:
            yield
    finally:
        current = _media_download_locks.get(video_id)
        if current is not None:
            existing_lock, count = current
            if count <= 1:
                _media_download_locks.pop(video_id, None)
            else:
                _media_download_locks[video_id] = (existing_lock, count - 1)


async def _evict_for_redownload(cache: CacheBackend, video_id: str) -> None:
    """Evict cached audio + file_id + chapters sidecar before a /redownload.

    Non-fatal: a failing evict still lets the fresh download overwrite the audio,
    at the cost of possibly-stale sidecars — we don't block on a flaky S3.
    """
    try:
        await cache.evict(video_id)
    except Exception:
        logger.warning(
            "Failed to evict cache for %s before redownload", video_id, exc_info=True
        )


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
    "• /chapters <URL> — browse a cached track's chapters as paginated "
    "captions with navigation buttons.\n"
)

# Only appended for TIMESTAMP_EDITOR_IDS. The command menu already hides
# /timestamps from everyone else, so telling non-editors about a command that
# will refuse them would undo that.
_HELP_TEXT_EDITOR = (
    "• /timestamps <URL> — followed by a pasted timestamp list, sets the "
    "chapters yourself when the uploader never published any. You can also "
    "reply to an audio message with /timestamps and the list to add them to "
    "that message in place.\n"
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
    if update.message is None or update.effective_user is None:
        return
    settings: Settings = context.bot_data["settings"]
    if not _is_user_allowed(update.effective_user.id, settings):
        logger.warning("Rejected /start from user %d", update.effective_user.id)
        return
    await update.message.reply_text(_WELCOME_TEXT)


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with help text."""
    if update.message is None or update.effective_user is None:
        return
    settings: Settings = context.bot_data["settings"]
    if not _is_user_allowed(update.effective_user.id, settings):
        logger.warning("Rejected /help from user %d", update.effective_user.id)
        return
    help_text = _HELP_TEXT
    if _is_timestamp_editor(update.effective_user.id, settings):
        help_text += _HELP_TEXT_EDITOR
    await update.message.reply_text(help_text)


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

    progress = ProgressManager(
        context.bot,
        chat_id=update.message.chat_id,
        reply_to_message_id=update.message.message_id,
    )
    await progress.create()

    video_id = parsed_url.video_id
    try:
        if video_id:
            # Evict + redownload under the per-media lock so the eviction is
            # serialized against any in-flight same-id request — otherwise it
            # could delete a cached file mid-upload for a concurrent resend.
            # Call _process_url_locked directly (we already hold the lock).
            async with _media_download_lock(video_id):
                await _evict_for_redownload(cache, video_id)
                await _process_url_locked(
                    update, context, progress, parsed_url, force_redownload=True
                )
        else:
            # Playlists have no top-level cache key — nothing to evict, no dedup.
            await _process_url_locked(
                update, context, progress, parsed_url, force_redownload=True
            )
    except Exception:
        logger.exception("Unexpected error in handle_redownload for user %d", user_id)
        with contextlib.suppress(Exception):
            await progress.set_step(
                Step.UPLOADING, StepStatus.ERROR, "An unexpected error occurred"
            )


# ---------------------------------------------------------------------------
# /timestamps command — hand-entered chapters for tracks the author never
# timestamped (the list usually lives in a comment).
# ---------------------------------------------------------------------------


_TIMESTAMPS_USAGE = (
    "Usage: /timestamps <URL>\n"
    "0:00 First track\n"
    "3:45 Second track\n"
    "1:02:10 Closer\n\n"
    "Paste the timestamp list on the lines after the URL.\n\n"
    "Or reply to an audio message with /timestamps and the list — the "
    "timestamps are added to that message in place, no URL needed.\n\n"
    "Add clear instead of a list to remove them and go back to the chapters "
    "the uploader published (if any)."
)


def _is_timestamp_editor(user_id: int, settings: Settings) -> bool:
    """Return True if *user_id* may write manual chapters.

    Fail-closed, unlike :func:`_is_user_allowed`: manual chapters are stored
    per video_id and shown to everyone who requests that track, so an empty
    TIMESTAMP_EDITOR_IDS means nobody can edit rather than everybody.
    """
    return user_id in settings.TIMESTAMP_EDITOR_IDS


async def handle_timestamps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Store hand-entered chapters for a track and resend it with them applied.

    The whole list arrives in one message: the command and URL on the first
    line, the pasted timestamps on the lines below. Storing replaces any
    previous set — there is no append or edit-by-index, so what the curator
    sees in the message is exactly what is stored.
    """
    if update.effective_user is None or update.message is None:
        return

    settings: Settings = context.bot_data["settings"]
    user_id: int = update.effective_user.id
    text: str = update.message.text or ""

    if not _is_user_allowed(user_id, settings):
        logger.warning("Rejected user %d (not in ALLOWED_USER_IDS)", user_id)
        return

    if not _is_timestamp_editor(user_id, settings):
        logger.warning("Rejected /timestamps from non-editor user %d", user_id)
        await update.message.reply_text(
            "You are not allowed to edit timestamps for this bot."
        )
        return

    first_line, _, body = text.partition("\n")
    cache: CacheBackend = context.bot_data["cache"]
    downloader: AudioDownloader = context.bot_data["downloader"]

    # Two ways in: name the track by URL, or reply to an audio message the bot
    # already sent. The reply form is the one you reach for when the track is
    # sitting right there in the chat.
    replied = update.message.reply_to_message
    replied_audio = getattr(replied, "audio", None) if replied is not None else None

    parsed_urls = extract_media_urls(first_line)
    parsed_url: ParsedURL | None = None
    video_id: str | None = None

    if parsed_urls:
        parsed_url = parsed_urls[0]
        # Manual chapters are keyed by the cache key, so the URL must resolve to
        # a concrete one. Playlists have no single key; on.soundcloud.com short
        # URLs only get theirs after a download resolves the slug, and storing
        # under a guess would file the list where nothing looks for it.
        if parsed_url.url_type == URLType.PLAYLIST:
            await update.message.reply_text(
                "/timestamps supports single tracks only (not playlists)."
            )
            return
        video_id = parsed_url.video_id
        if not video_id:
            await update.message.reply_text(
                "Please use the full track URL — this short link has no cache key "
                "until it is resolved."
            )
            return
    elif replied_audio is not None:
        video_id = await cache.find_by_file_id(replied_audio.file_id)
        if video_id is None:
            await update.message.reply_text(
                "I can't tell which track that message is — it may predate the "
                "current cache entry. Send /timestamps with the track URL instead."
            )
            return
    else:
        await update.message.reply_text(_TIMESTAMPS_USAGE)
        return

    if not await _consume_rate_limit(user_id, settings):
        await update.message.reply_text(
            "You have reached the rate limit. Please wait a minute."
        )
        return

    # --- Clear mode -------------------------------------------------------
    if body.strip() == "" and first_line.strip().lower().endswith(" clear"):
        async with _media_download_lock(video_id):
            removed = await cache.delete_manual_chapters(video_id)
            # Fall back to whatever the uploader published, if anything.
            remaining = await cache.get_chapters(video_id)
            if removed and replied is not None and parsed_url is None:
                # The message is still showing the list we just deleted.
                await _apply_chapters_in_place(
                    context,
                    replied,
                    video_id=video_id,
                    title=_usable_track_title(
                        getattr(replied_audio, "title", None), video_id
                    )
                    or _extract_chapter_page_title(getattr(replied, "caption", None))
                    or video_id,
                    chapters=remaining,
                    paginate=settings.CHAPTER_PAGES_ENABLED,
                )
        await update.message.reply_text(
            "Removed your timestamps for this track."
            if removed
            else "No hand-entered timestamps were stored for this track."
        )
        return

    if body.strip() == "":
        await update.message.reply_text(_TIMESTAMPS_USAGE)
        return

    # --- Parse ------------------------------------------------------------
    # Duration bounds the timestamps. The reply form already has it (and the
    # title) on the audio message, so it costs nothing there; the URL form has
    # to ask yt-dlp, which can fail for reasons that have nothing to do with
    # the pasted list — treat that as best-effort and parse without the bound.
    duration: int | None = None
    display_title: str | None = None
    if parsed_url is None and replied_audio is not None:
        duration = replied_audio.duration or None
        display_title = _usable_track_title(replied_audio.title, video_id) or (
            _extract_chapter_page_title(getattr(replied, "caption", None))
        )
    elif parsed_url is not None:
        try:
            metadata = await downloader.fetch_metadata(parsed_url)
        except (VideoUnavailableError, DownloadError):
            logger.info(
                "Could not fetch metadata for /timestamps on %s",
                video_id,
                exc_info=True,
            )
        else:
            duration = metadata.duration_seconds
            display_title = clean_title(metadata.title) or None

    try:
        chapters = parse_timestamps(body, duration_seconds=duration)
    except TimestampParseError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return

    # --- Store, then show the result --------------------------------------
    # The lock spans store → exists → resend: a concurrent /redownload evicting
    # in the middle would otherwise produce a message showing chapters that are
    # no longer stored, attached to audio that was just replaced.
    async with _media_download_lock(video_id):
        try:
            await cache.store_manual_chapters(video_id, chapters)
        except Exception:
            logger.exception("Failed to store manual chapters for %s", video_id)
            await update.message.reply_text(
                "❌ Could not save the timestamps. Please try again."
            )
            return

        # --- Reply form: edit the message that is already in the chat ----
        # No resend — the point of replying to the audio is to see the
        # timestamps appear on that message.
        if replied is not None and parsed_url is None:
            edited = await _apply_chapters_in_place(
                context,
                replied,
                video_id=video_id,
                title=display_title or video_id,
                chapters=chapters,
                paginate=settings.CHAPTER_PAGES_ENABLED,
            )
            await update.message.reply_text(
                f"✅ Added {len(chapters)} timestamps."
                if edited
                else f"✅ Saved {len(chapters)} timestamps, but that message "
                "could not be edited. Send the link to get a fresh copy."
            )
            return

        if not await cache.exists(video_id):
            await update.message.reply_text(
                f"✅ Saved {len(chapters)} timestamps. Send the link to download "
                "the track and they will be applied."
            )
            return

        progress = ProgressManager(
            context.bot,
            chat_id=update.message.chat_id,
            reply_to_message_id=update.message.message_id,
        )
        await progress.create()
        try:
            await progress.set_step(Step.DOWNLOADING, StepStatus.DONE, "Found in cache")
            await progress.set_step(
                Step.PROCESSING, StepStatus.DONE, "Timestamps saved"
            )
            if display_title is None:
                cached_path = await cache.get(video_id)
                if cached_path is not None:
                    cached_title, _ = _extract_audio_metadata(cached_path)
                    display_title = _usable_track_title(cached_title, video_id)
            await _resend_cached_audio(
                update,
                context,
                progress,
                parsed_url=parsed_url,
                video_id=video_id,
                display_title=display_title or video_id,
                chapters=chapters,
                # We hold the per-media lock; the download fallback would
                # deadlock trying to take it again.
                allow_download_fallback=False,
            )
        except Exception:
            logger.exception(
                "Unexpected error in handle_timestamps for user %d", user_id
            )
            with contextlib.suppress(Exception):
                await progress.set_step(
                    Step.UPLOADING, StepStatus.ERROR, "An unexpected error occurred"
                )


# ---------------------------------------------------------------------------
# Chapter resolution + cached-audio resend, shared by /refresh and /timestamps
# ---------------------------------------------------------------------------


async def _apply_chapters_in_place(
    context: ContextTypes.DEFAULT_TYPE,
    message: Message,
    *,
    video_id: str,
    title: str,
    chapters: tuple[tuple[int, str], ...] | None,
    paginate: bool,
) -> bool:
    """Rewrite an existing audio message's caption to show *chapters*.

    An empty *chapters* rewrites the caption back to a bare title, which is
    what /timestamps ... clear needs: the message must stop showing a list
    that is no longer stored.

    Returns False when Telegram refuses the edit (message too old, not ours,
    caption unchanged) so the caller can say so — the chapters are stored
    either way and will show up on the next send.
    """
    audio_message = _build_audio_message(title, chapters, video_id, paginate=paginate)
    try:
        await context.bot.edit_message_caption(
            chat_id=message.chat_id,
            message_id=message.message_id,
            caption=audio_message.caption,
            reply_markup=audio_message.reply_markup,
        )
    except TelegramError:
        # Network errors and timeouts land here too: the chapters are already
        # stored, so the caller reports "saved but not shown" rather than
        # letting the failure escape as an unhandled error.
        logger.warning(
            "Could not edit caption of message %s for %s",
            getattr(message, "message_id", "?"),
            video_id,
            exc_info=True,
        )
        return False

    # Overflow tiers put the rest of the list in follow-up messages; hang them
    # off the audio message so the whole list stays together.
    if audio_message.index_messages:
        await _send_chapter_index(
            context.bot,
            message.chat_id,
            message.message_id,
            audio_message.index_messages,
        )
    return True


async def _manual_chapters(
    cache: CacheBackend, video_id: str
) -> tuple[tuple[int, str], ...] | None:
    """Read hand-entered chapters, treating a read failure as "none stored".

    Returns None when nothing is stored, which is distinct from an empty tuple
    (a deliberate "this track has no chapters" override).
    """
    try:
        return await cache.get_manual_chapters(video_id)
    except Exception:
        logger.warning("Failed to read manual chapters for %s", video_id, exc_info=True)
        return None


async def _effective_chapters(
    cache: CacheBackend,
    video_id: str,
    upstream: tuple[tuple[int, str], ...] | None = None,
) -> tuple[tuple[int, str], ...] | None:
    """Return the chapters to display for *video_id*.

    Hand-entered chapters (/timestamps) win over whatever yt-dlp reported,
    including when the stored override is empty — that is a deliberate "this
    track has no chapters", not an absent value.

    *upstream* is the already-known yt-dlp chapter set; when omitted it is read
    from the cache. Callers that need to compare upstream against upstream
    (/refresh) must read ``cache.get_chapters`` directly instead of this.
    """
    manual = await _manual_chapters(cache, video_id)
    if manual is not None:
        return manual or None
    if upstream is not None:
        return upstream
    return await cache.get_chapters(video_id)


async def _resend_cached_audio(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    progress: ProgressManager,
    *,
    parsed_url: ParsedURL | None,
    video_id: str,
    display_title: str,
    chapters: tuple[tuple[int, str], ...] | None,
    allow_download_fallback: bool = True,
) -> None:
    """Resend already-cached audio with a freshly built caption.

    Prefers the stored Telegram file_id, falls back to re-uploading the cached
    file, and falls through to a fresh download if the cache entry turns out to
    be gone. Drives *progress* to completion.

    *allow_download_fallback* must be False when the caller already holds the
    per-media lock: the fresh-download path takes that same non-reentrant lock
    and would deadlock. Such callers get an error message instead, and may pass
    *parsed_url* as None since only that path uses it.
    """
    if update.message is None:
        return

    settings: Settings = context.bot_data["settings"]
    cache: CacheBackend = context.bot_data["cache"]
    paginate = settings.CHAPTER_PAGES_ENABLED
    audio_message = _build_audio_message(
        display_title, chapters, video_id, paginate=paginate
    )

    # --- Fast path: resend via Telegram file_id ---------------------------
    file_id = await cache.get_file_id(video_id)
    sent_msg: Message | None = None
    if file_id:
        try:
            await progress.set_step(Step.UPLOADING, StepStatus.ACTIVE)
            await progress.start_upload_animation()
            sent_msg = await context.bot.send_audio(
                chat_id=update.message.chat_id,
                audio=file_id,
                caption=audio_message.caption,
                reply_markup=audio_message.reply_markup,
                read_timeout=300,
                write_timeout=300,
            )
        except Exception:
            logger.warning(
                "file_id resend failed for %s, falling back to upload",
                video_id,
            )
            sent_msg = None

    # --- Fallback: re-upload from cached file -----------------------------
    if sent_msg is None:
        cached_path: Path | None = await cache.get(video_id)
        if cached_path is None:
            # Stale cache: exists() said yes but the file is gone
            # (S3 eviction, race, etc.). Recover by downloading fresh
            # instead of surfacing an error — matches _process_url's
            # behavior in the same state.
            logger.warning(
                "Resend: cache.exists()=True but cache.get()=None for %s — "
                "falling through to fresh download",
                video_id,
            )
            # Reset any in-flight upload animation/state from the failed
            # file_id attempt so _process_url starts with a clean slate
            # (otherwise the UI shows upload-active while download runs).
            await progress.stop_upload_animation()
            await progress.set_step(Step.PROCESSING, StepStatus.PENDING)
            await progress.set_step(Step.UPLOADING, StepStatus.PENDING)
            if not allow_download_fallback or parsed_url is None:
                await progress.set_step(
                    Step.UPLOADING,
                    StepStatus.ERROR,
                    "The cached copy is gone — send the link to download it again",
                )
                return
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
            chapters=chapters,
        )
        sent_msg = await _send_audio(
            context.bot, update.message.chat_id, result, progress, paginate=paginate
        )
        if sent_msg.audio:
            try:
                await cache.store_file_id(video_id, sent_msg.audio.file_id)
            except Exception:
                logger.warning(
                    "Failed to store file_id for %s after resend",
                    video_id,
                    exc_info=True,
                )
    elif audio_message.index_messages:
        # file_id path succeeded — chapter index reply still needed if overflow
        await _send_chapter_index(
            context.bot,
            update.message.chat_id,
            sent_msg.message_id,
            audio_message.index_messages,
        )

    await progress.set_step(Step.UPLOADING, StepStatus.DONE)
    await asyncio.sleep(2)
    await progress.delete()


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
        # Upstream chapters were refreshed above, but a hand-entered set still
        # wins for display — /refresh must not undo /timestamps.
        display_chapters = await _effective_chapters(cache, video_id, final_chapters)
        await _resend_cached_audio(
            update,
            context,
            progress,
            parsed_url=parsed_url,
            video_id=video_id,
            display_title=display_title,
            chapters=display_chapters,
        )
    except Exception:
        logger.exception("Unexpected error in handle_refresh for user %d", user_id)
        with contextlib.suppress(Exception):
            await progress.set_step(
                Step.UPLOADING, StepStatus.ERROR, "An unexpected error occurred"
            )


_CHAPTERS_USAGE = (
    "Usage: /chapters <YouTube or SoundCloud URL>\n"
    "Paginated chapter captions. Works only for cached single tracks."
)


async def handle_chapters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send cached audio with paginated chapter captions.

    This is intentionally cache-only: the command may fetch metadata, but it
    never downloads audio from the source. Normal URL handling remains the
    stable path.
    """
    if update.effective_user is None or update.message is None:
        return

    settings: Settings = context.bot_data["settings"]
    if not settings.CHAPTER_PAGES_ENABLED:
        await update.message.reply_text("Chapter pages are disabled.")
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

    chapters = await _effective_chapters(cache, video_id)
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
    if not settings.CHAPTER_PAGES_ENABLED:
        await query.answer("Chapter pages are disabled.", show_alert=True)
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
        chapters = await _effective_chapters(cache, video_id)
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
    """Serialize same-video processing, then run the cache/download/upload flow.

    Single videos hold a per-media lock for the whole sequence so two concurrent
    requests for the same uncached URL don't both download it — the second waits
    and then hits the cache the first populated. Playlists have no single cache
    key and are not deduplicated.

    The lock intentionally spans the entire cache-check → download → upload
    sequence, not just the download: same-id requests are rare, and holding it
    through the upload keeps the dedup logic simple and guarantees the second
    request sees a fully durable cache entry (audio + file_id + chapters) rather
    than racing a half-written one.
    """
    video_id = parsed_url.video_id
    if video_id is None:
        await _process_url_locked(
            update, context, progress, parsed_url, force_redownload
        )
        return
    async with _media_download_lock(video_id):
        await _process_url_locked(
            update, context, progress, parsed_url, force_redownload
        )


async def _process_url_locked(
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
    paginate = settings.CHAPTER_PAGES_ENABLED

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
                fid_chapters = await _effective_chapters(cache, video_id)
                fid_path = await cache.get(video_id)
                fid_title = video_id
                if fid_path:
                    t, _ = _extract_audio_metadata(fid_path)
                    if t:
                        fid_title = clean_title(t) or video_id
                fid_audio_message = _build_audio_message(
                    fid_title, fid_chapters, video_id, paginate=paginate
                )
                fid_msg = await context.bot.send_audio(
                    chat_id=update.message.chat_id,
                    audio=file_id,
                    caption=fid_audio_message.caption,
                    reply_markup=fid_audio_message.reply_markup,
                    read_timeout=300,
                    write_timeout=300,
                )
                if fid_audio_message.index_messages:
                    await _send_chapter_index(
                        context.bot,
                        update.message.chat_id,
                        fid_msg.message_id,
                        fid_audio_message.index_messages,
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
            cached_chapters = await _effective_chapters(cache, video_id)
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
                context.bot, update.message.chat_id, result, progress, paginate=paginate
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
                paginate=paginate,
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
            paginate=paginate,
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
    *,
    paginate: bool,
) -> None:
    """Cache a DownloadResult then upload it to Telegram."""
    stored_path: Path | None = None
    try:
        stored_path = await cache.put(result.video_id, result.file_path)
    except Exception:
        logger.exception("Cache put failed for video_id=%s", result.video_id)

    # Persist chapters BEFORE sending so the page-navigation callback can always
    # rebuild pages from the cache. If the store fails, fall back to a non-paginated
    # message rather than shipping buttons whose callback would find no chapters.
    chapters_stored = False
    if result.chapters:
        try:
            await cache.store_chapters(result.video_id, result.chapters)
            chapters_stored = True
        except Exception:
            logger.warning(
                "Failed to store chapters for %s", result.video_id, exc_info=True
            )

    # Hand-entered chapters outlive the audio they describe (they survive LRU
    # eviction), so a track downloaded again still shows the curated list.
    manual = await _manual_chapters(cache, result.video_id)
    if manual is not None:
        display_chapters = manual or None
        # The override lives in its own sidecar, so page callbacks can rebuild
        # from it whether or not the upstream chapter store succeeded.
        chapters_stored = True
    else:
        display_chapters = result.chapters

    send_result = dataclasses.replace(
        result,
        file_path=stored_path or result.file_path,
        chapters=display_chapters,
    )
    msg = await _send_audio(
        bot, chat_id, send_result, progress, paginate=paginate and chapters_stored
    )
    if msg.audio:
        try:
            await cache.store_file_id(result.video_id, msg.audio.file_id)
        except Exception:
            logger.warning(
                "Failed to store file_id for %s — fast resend unavailable",
                result.video_id,
                exc_info=True,
            )


async def _send_audio(
    bot: Bot,
    chat_id: int,
    result: DownloadResult,
    progress: ProgressManager,
    *,
    paginate: bool,
) -> Message:
    """Send a single audio file to the user. Returns the sent Message."""
    await progress.set_step(Step.PROCESSING, StepStatus.ACTIVE)
    await progress.start_animation(Step.PROCESSING)
    display_title = clean_title(result.title) or result.video_id
    safe_filename = sanitize_filename(display_title) or result.video_id
    thumbnail = _extract_thumbnail(result.file_path)
    audio_message = _build_audio_message(
        display_title, result.chapters, result.video_id, paginate=paginate
    )
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
            caption=audio_message.caption,
            reply_markup=audio_message.reply_markup,
            filename=f"{safe_filename}{result.file_path.suffix}",
            read_timeout=300,
            write_timeout=300,
        )
    if audio_message.index_messages:
        await _send_chapter_index(
            bot, chat_id, msg.message_id, audio_message.index_messages
        )
    return msg


async def _edit_error(bot: Bot, progress: ProgressManager, text: str) -> None:
    """Force-update the progress message with an error summary."""
    try:
        await progress.edit_text(text)
    except Exception:
        logger.exception("Failed to edit error message")
