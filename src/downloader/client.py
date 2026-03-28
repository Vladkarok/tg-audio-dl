"""yt-dlp audio downloader wrapper.

All yt-dlp operations run in asyncio.to_thread because yt-dlp is synchronous.
Progress hooks are bridged from yt-dlp's sync callback to an async callback via
asyncio.run_coroutine_threadsafe.

Security notes:
- track_id (cache key) is validated against _TRACK_ID_RE before use.
- Output template uses %(id)s — yt-dlp controls the filename; no user input
  reaches the filesystem path directly.
- URLs are passed to YoutubeDL() Python API only; never to a shell subprocess.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse as _urlparse

import yt_dlp

from src.downloader.url_parser import ParsedURL, Platform, URLType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public data models
# ---------------------------------------------------------------------------

# Accepts YouTube 11-char IDs, SoundCloud numeric IDs, and sc_slug cache keys
_TRACK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# (start_seconds, title)
Chapter = tuple[int, str]


@dataclass(frozen=True)
class DownloadResult:
    """Metadata and location of a successfully downloaded audio file."""

    file_path: Path
    video_id: str
    title: str
    artist: str | None  # uploader/channel
    duration_seconds: int | None
    thumbnail_url: str | None
    file_size_bytes: int
    chapters: tuple[Chapter, ...] | None = None


@dataclass(frozen=True)
class DownloadProgress:
    """Snapshot of download progress emitted by the progress hook."""

    status: str  # "downloading" | "processing" | "done" | "error"
    percentage: float | None  # 0.0–100.0, None if unknown
    speed_bps: float | None  # bytes/sec, None if unknown
    eta_seconds: int | None
    filename: str | None


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class DownloadError(Exception):
    """Base download error."""


class VideoUnavailableError(DownloadError):
    """Video is private, deleted, or geo-blocked."""


class FileTooLargeError(DownloadError):
    """Downloaded file exceeds max_file_size_bytes."""

    def __init__(self, file_size_bytes: int, max_bytes: int) -> None:
        self.file_size_bytes = file_size_bytes
        self.max_bytes = max_bytes
        super().__init__(
            f"File size {file_size_bytes} bytes exceeds limit of {max_bytes} bytes"
        )


# ---------------------------------------------------------------------------
# Type alias
# ---------------------------------------------------------------------------

ProgressCallback = Callable[[DownloadProgress], Coroutine[Any, Any, None]]
TrackStartCallback = Callable[[int, int, str], Coroutine[Any, Any, None]]
TrackReadyCallback = Callable[["DownloadResult"], Coroutine[Any, Any, None]]


async def _run_blocking[**P, T](
    func: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
) -> T:
    """Run blocking work off the event loop.

    Kept as a small wrapper so tests can replace the thread offload strategy
    without patching ``asyncio`` globally.
    """
    return await asyncio.to_thread(func, *args, **kwargs)


# ---------------------------------------------------------------------------
# AudioDownloader
# ---------------------------------------------------------------------------


class AudioDownloader:
    """Async wrapper around yt-dlp for downloading YouTube and SoundCloud audio."""

    def __init__(
        self,
        download_dir: Path,
        max_file_size_bytes: int,
        proxy_url: str | None = None,
        cookies_file: str | None = None,
        max_concurrent_downloads: int = 3,
        download_timeout: int = 1800,
    ) -> None:
        self._download_dir = download_dir
        self._max_file_size_bytes = max_file_size_bytes
        self._proxy_url = proxy_url
        self._cookies_file = cookies_file
        self._semaphore = asyncio.Semaphore(max_concurrent_downloads)
        self._download_timeout = download_timeout
        # Per-media lock + refcount to prevent concurrent downloads of the
        # same ID.  The refcount tracks how many callers are using (or waiting
        # on) a given lock so we only clean up once the last one leaves.
        self._inflight: dict[str, tuple[asyncio.Lock, int]] = {}
        # Protects all reads and writes to _inflight to make refcount
        # increments and decrements atomic.
        self._inflight_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def download(
        self,
        parsed_url: ParsedURL,
        progress_callback: ProgressCallback | None = None,
        max_tracks: int = 50,
        track_start_callback: TrackStartCallback | None = None,
        track_ready_callback: TrackReadyCallback | None = None,
    ) -> list[DownloadResult]:
        """Download audio for a ParsedURL.

        - SINGLE / RADIO_MIX: returns list with one DownloadResult.
        - PLAYLIST: returns one DownloadResult per track (up to max_tracks).

        Raises DownloadError (or subclass) on failure.
        """
        media_key = parsed_url.video_id or parsed_url.canonical_url
        # Register interest (bump refcount) atomically before acquiring the
        # per-media lock.  _inflight_lock is held only for the brief dict
        # operations; it is NOT held during the actual download.
        async with self._inflight_lock:
            if media_key in self._inflight:
                lock, count = self._inflight[media_key]
                self._inflight[media_key] = (lock, count + 1)
            else:
                lock = asyncio.Lock()
                self._inflight[media_key] = (lock, 1)

        try:
            async with lock, self._semaphore:
                return await self._download_inner(
                    parsed_url,
                    progress_callback,
                    max_tracks,
                    track_start_callback,
                    track_ready_callback,
                )
        finally:
            # Decrement refcount atomically; remove entry only when no one
            # else is using or waiting on this lock.  Wrapping the lock
            # acquisition ensures cancellation during the wait still
            # decrements the counter.
            async with self._inflight_lock:
                entry = self._inflight.get(media_key)
                if entry is not None:
                    _, count = entry
                    if count <= 1:
                        self._inflight.pop(media_key, None)
                    else:
                        self._inflight[media_key] = (lock, count - 1)

    async def _download_inner(
        self,
        parsed_url: ParsedURL,
        progress_callback: ProgressCallback | None = None,
        max_tracks: int = 50,
        track_start_callback: TrackStartCallback | None = None,
        track_ready_callback: TrackReadyCallback | None = None,
    ) -> list[DownloadResult]:
        """Inner download logic, runs under the concurrency semaphore."""
        loop = asyncio.get_running_loop()

        if parsed_url.url_type == URLType.PLAYLIST:
            return await self._download_playlist(
                parsed_url.canonical_url,
                max_tracks,
                progress_callback,
                loop,
                track_start_callback=track_start_callback,
                track_ready_callback=track_ready_callback,
            )

        # SINGLE or RADIO_MIX
        # For YouTube: video_id is the 11-char yt ID — used for both file
        #   lookup and as the cache key.
        # For SoundCloud: video_id is our sc_slug cache key; yt-dlp uses its
        #   own numeric ID for the file — so we pass yt_id=None (unknown) and
        #   cache_id=sc_slug so _build_result uses the slug as the result key.
        if parsed_url.platform == Platform.SOUNDCLOUD:
            yt_id = None  # yt-dlp will use the numeric SC track ID for the file
            cache_id = parsed_url.video_id  # sc_slug or None for short URLs
        else:
            yt_id = parsed_url.video_id  # YouTube 11-char ID
            cache_id = None  # use yt-dlp's ID (same thing)

        result = await self._download_one(
            url=parsed_url.canonical_url,
            yt_id=yt_id,
            cache_id=cache_id,
            noplaylist=True,
            progress_callback=progress_callback,
            loop=loop,
        )
        return [result]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _download_one(
        self,
        url: str,
        yt_id: str | None,
        cache_id: str | None,
        noplaylist: bool,
        progress_callback: ProgressCallback | None,
        loop: asyncio.AbstractEventLoop,
    ) -> DownloadResult:
        """Run yt-dlp for a single URL and return a DownloadResult.

        yt_id    — yt-dlp's expected track ID (used for cleanup on error).
                   None for SoundCloud where the numeric ID is unknown pre-download.
        cache_id — override for the result's video_id (our cache key).
                   None means use whatever yt-dlp returns as info['id'].
        """
        ydl_opts = self._build_opts(
            noplaylist=noplaylist,
            progress_callback=progress_callback,
            loop=loop,
        )

        try:
            info = await asyncio.wait_for(
                _run_blocking(self._run_ydl, ydl_opts, url),
                timeout=self._download_timeout,
            )
        except TimeoutError as exc:
            self._cleanup_partials(yt_id)
            raise DownloadError(
                f"Download timed out after {self._download_timeout}s"
            ) from exc
        except VideoUnavailableError:
            self._cleanup_partials(yt_id)
            raise

        return self._build_result(info, cache_id=cache_id)

    async def _fetch_playlist_metadata(
        self,
        url: str,
        max_tracks: int,
        loop: asyncio.AbstractEventLoop,
    ) -> list[tuple[str, str]]:
        """Fetch playlist entry IDs and titles without downloading audio.

        Uses yt-dlp's extract_flat mode for a fast single API call.
        Returns a list of (track_id, title) pairs capped at max_tracks.
        """
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
            "noplaylist": False,
            "playlistend": max_tracks,
        }
        if self._proxy_url is not None:
            opts["proxy"] = self._proxy_url
        if self._cookies_file is not None:
            opts["cookiefile"] = self._cookies_file

        try:
            info = await asyncio.wait_for(
                _run_blocking(self._run_ydl_flat, opts, url),
                timeout=self._download_timeout,
            )
        except TimeoutError as exc:
            raise DownloadError("Timed out fetching playlist metadata") from exc

        raw_entries = info.get("entries") or []
        entries: list[tuple[str, str]] = []
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("id") or entry.get("url", "")
            if not entry_id or not _TRACK_ID_RE.fullmatch(entry_id):
                # For entries where id is a full URL (SoundCloud), use url field
                entry_url = entry.get("url", "")
                if entry_url.startswith(("http://", "https://")):
                    entry_id = entry_url
                else:
                    continue
            title = entry.get("title") or entry_id
            entries.append((entry_id, title))

        return entries[:max_tracks]

    async def _download_playlist(
        self,
        url: str,
        max_tracks: int,
        progress_callback: ProgressCallback | None,
        loop: asyncio.AbstractEventLoop,
        track_start_callback: TrackStartCallback | None = None,
        track_ready_callback: TrackReadyCallback | None = None,
    ) -> list[DownloadResult]:
        """Download each playlist track individually, firing callbacks per track."""
        entries = await self._fetch_playlist_metadata(url, max_tracks, loop)
        if not entries:
            raise DownloadError("Playlist is empty or unavailable")

        total = len(entries)
        results: list[DownloadResult] = []

        for idx, (track_id, title) in enumerate(entries, start=1):
            if track_start_callback is not None:
                try:
                    await track_start_callback(idx, total, title)
                except Exception:
                    logger.warning(
                        "track_start_callback raised for track %d/%d", idx, total
                    )

            # Determine the URL to pass to yt-dlp
            if track_id.startswith(("http://", "https://")):
                track_url = track_id
                yt_id = None
            else:
                track_url = f"https://www.youtube.com/watch?v={track_id}"
                yt_id = track_id

            try:
                result = await self._download_one(
                    url=track_url,
                    yt_id=yt_id,
                    cache_id=None,
                    noplaylist=True,
                    progress_callback=progress_callback,
                    loop=loop,
                )
            except FileTooLargeError as exc:
                logger.warning("Skipping playlist track %s: %s", track_id, exc)
                continue
            except DownloadError:
                logger.warning("Skipping failed playlist track %s", track_id)
                continue

            results.append(result)

            if track_ready_callback is not None:
                try:
                    await track_ready_callback(result)
                except Exception:
                    logger.warning(
                        "track_ready_callback raised for track %d/%d", idx, total
                    )

        if not results and entries:
            raise DownloadError(
                f"All {total} playlist entries were unavailable or failed"
            )
        return results

    # ------------------------------------------------------------------
    # yt-dlp orchestration (synchronous — runs inside to_thread)
    # ------------------------------------------------------------------

    def _run_ydl(self, ydl_opts: dict[str, Any], url: str) -> dict[str, Any]:
        """Call yt-dlp synchronously; translate errors to our hierarchy."""
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info: dict[str, Any] = ydl.extract_info(url, download=True)
            return info
        except (
            yt_dlp.utils.DownloadError,
            yt_dlp.utils.ExtractorError,
            yt_dlp.utils.UnsupportedError,
        ) as exc:
            raise VideoUnavailableError(self._sanitize_error(str(exc))) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise DownloadError(
                self._sanitize_error(f"Unexpected download error: {exc}")
            ) from exc

    def _run_ydl_flat(self, ydl_opts: dict[str, Any], url: str) -> dict[str, Any]:
        """Call yt-dlp synchronously with extract_flat; no audio downloaded."""
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info: dict[str, Any] = ydl.extract_info(url, download=False)
            return info
        except (
            yt_dlp.utils.DownloadError,
            yt_dlp.utils.ExtractorError,
            yt_dlp.utils.UnsupportedError,
        ) as exc:
            raise DownloadError(
                self._sanitize_error(f"Failed to fetch playlist metadata: {exc}")
            ) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise DownloadError(
                self._sanitize_error(
                    f"Unexpected error fetching playlist metadata: {exc}"
                )
            ) from exc

    # ------------------------------------------------------------------
    # DownloadResult construction
    # ------------------------------------------------------------------

    def _build_result(
        self, info: dict[str, Any], cache_id: str | None = None
    ) -> DownloadResult:
        """Build a DownloadResult from a yt-dlp info_dict.

        cache_id — if provided, used as result.video_id (our cache key).
                   The yt-dlp info['id'] is still used to locate the file on disk.
        """
        ydl_id = info.get("id", "")
        # Validate yt-dlp's ID first — it is used for filesystem operations
        if not _TRACK_ID_RE.fullmatch(ydl_id):
            raise DownloadError(f"yt-dlp returned unexpected video id: {ydl_id!r}")

        # File on disk always uses yt-dlp's own ID
        file_path = self._find_audio_file(ydl_id)
        try:
            file_size = file_path.stat().st_size
        except FileNotFoundError as exc:
            raise DownloadError(
                f"Downloaded file disappeared before processing for id={ydl_id!r}"
            ) from exc

        if file_size > self._max_file_size_bytes:
            raise FileTooLargeError(file_size, self._max_file_size_bytes)

        # The result's video_id is the cache key: caller override or yt-dlp ID
        result_id = cache_id if cache_id is not None else ydl_id

        artist = info.get("uploader") or info.get("channel") or None

        # Only accept http(s) thumbnail URLs — reject data:, file:, etc.
        raw_thumb = info.get("thumbnail")
        thumbnail_url = (
            raw_thumb
            if isinstance(raw_thumb, str)
            and raw_thumb.startswith(("http://", "https://"))
            else None
        )

        # Extract chapters if available
        raw_chapters = info.get("chapters")
        chapters: tuple[Chapter, ...] | None = None
        if raw_chapters:
            chapters = (
                tuple(
                    (max(0, int(ch["start_time"])), ch["title"])
                    for ch in raw_chapters
                    if isinstance(ch.get("start_time"), (int, float))
                    and isinstance(ch.get("title"), str)
                    and ch["title"].strip()  # Skip blank chapter titles
                )
                or None
            )

        return DownloadResult(
            file_path=file_path,
            video_id=result_id,
            title=info["title"],
            artist=artist,
            duration_seconds=info.get("duration"),
            thumbnail_url=thumbnail_url,
            file_size_bytes=file_size,
            chapters=chapters,
        )

    def _find_audio_file(self, ydl_id: str) -> Path:
        """Locate the downloaded audio file for *ydl_id* in download_dir."""
        for ext in ("m4a", "webm", "opus", "mp3", "ogg"):
            candidate = self._download_dir / f"{ydl_id}.{ext}"
            if candidate.exists():
                return candidate

        matches = list(self._download_dir.glob(f"{ydl_id}.*"))
        if matches:
            return matches[0]

        raise DownloadError(
            f"Downloaded file not found for id={ydl_id!r} in {self._download_dir}"
        )

    # ------------------------------------------------------------------
    # yt-dlp options builder
    # ------------------------------------------------------------------

    def _build_opts(
        self,
        noplaylist: bool,
        progress_callback: ProgressCallback | None,
        loop: asyncio.AbstractEventLoop,
        playlistend: int | None = None,
    ) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "format": "bestaudio[ext=m4a]/bestaudio",
            "outtmpl": str(self._download_dir / "%(id)s.%(ext)s"),
            "writethumbnail": True,
            "embedchapters": True,
            "postprocessors": [
                # 1. Convert WebP thumbnail to JPG (must precede embed)
                {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
                # 2. Write metadata tags first (ffmpeg -vn strips cover art)
                {"key": "FFmpegMetadata"},
                # 3. Embed thumbnail LAST via mutagen (survives ffmpeg)
                {"key": "EmbedThumbnail"},
            ],
            "quiet": True,
            "no_warnings": True,
            "noplaylist": noplaylist,
            "progress_hooks": [self._make_sync_progress_hook(progress_callback, loop)],
            # Network-level timeout backstop — ensures the yt-dlp thread unblocks
            # even if asyncio.wait_for fires first (the thread keeps running until
            # a blocking network call returns; socket_timeout bounds that wait).
            "socket_timeout": self._download_timeout,
            # Explicitly enable Node.js for YouTube JS signature challenges
            "js_runtimes": {"node": {}},
        }
        if self._proxy_url is not None:
            opts["proxy"] = self._proxy_url
        if self._cookies_file is not None:
            opts["cookiefile"] = self._cookies_file
        if playlistend is not None:
            opts["playlistend"] = playlistend
        return opts

    # ------------------------------------------------------------------
    # Progress hook bridge: sync (yt-dlp) → async (caller)
    # ------------------------------------------------------------------

    @staticmethod
    def _make_sync_progress_hook(
        callback: ProgressCallback | None,
        loop: asyncio.AbstractEventLoop,
    ) -> Callable[[dict[str, Any]], None]:
        """Return a sync hook that dispatches to *callback* on *loop*."""

        def hook(d: dict[str, Any]) -> None:
            if callback is None:
                return

            downloaded = d.get("downloaded_bytes")
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            percentage: float | None = None
            if downloaded is not None and total:
                percentage = downloaded / total * 100.0

            progress = DownloadProgress(
                status=d.get("status", "unknown"),
                percentage=percentage,
                speed_bps=d.get("speed"),
                eta_seconds=d.get("eta"),
                filename=d.get("filename"),
            )
            asyncio.run_coroutine_threadsafe(callback(progress), loop)

        return hook

    # ------------------------------------------------------------------
    # Cleanup helpers
    # ------------------------------------------------------------------

    def _sanitize_error(self, msg: str) -> str:
        """Strip proxy credentials from error messages."""
        if self._proxy_url and "@" in self._proxy_url:
            msg = msg.replace(self._proxy_url, "<proxy>")
            # Also strip just the userinfo part
            with contextlib.suppress(Exception):
                p = _urlparse(self._proxy_url)
                if p.username:
                    if p.password:
                        cred = f"{p.username}:{p.password}@"
                    else:
                        cred = f"{p.username}@"
                    msg = msg.replace(cred, "")
        return msg

    def _cleanup_partials(self, yt_id: str | None) -> None:
        """Remove any partial files for *yt_id* from download_dir."""
        if not yt_id:
            return
        for path in self._download_dir.glob(f"{yt_id}.*"):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
