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
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yt_dlp

from src.downloader.url_parser import ParsedURL, Platform, URLType

# ---------------------------------------------------------------------------
# Public data models
# ---------------------------------------------------------------------------

# Accepts YouTube 11-char IDs, SoundCloud numeric IDs, and sc_slug cache keys
_TRACK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Keep for download_single() which is YouTube-only
_YT_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


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


# ---------------------------------------------------------------------------
# AudioDownloader
# ---------------------------------------------------------------------------


class AudioDownloader:
    """Async wrapper around yt-dlp for downloading YouTube and SoundCloud audio."""

    def __init__(
        self,
        download_dir: Path,
        max_file_size_bytes: int,
        cookies_file: Path | None = None,
    ) -> None:
        self._download_dir = download_dir
        self._max_file_size_bytes = max_file_size_bytes
        self._cookies_file = cookies_file

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def download(
        self,
        parsed_url: ParsedURL,
        progress_callback: ProgressCallback | None = None,
        max_tracks: int = 50,
    ) -> list[DownloadResult]:
        """Download audio for a ParsedURL.

        - SINGLE / RADIO_MIX: returns list with one DownloadResult.
        - PLAYLIST: returns one DownloadResult per track (up to max_tracks).

        Raises DownloadError (or subclass) on failure.
        """
        loop = asyncio.get_running_loop()

        if parsed_url.url_type == URLType.PLAYLIST:
            return await self._download_playlist(
                parsed_url.canonical_url, max_tracks, progress_callback, loop
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

    async def download_single(
        self,
        video_id: str,
        progress_callback: ProgressCallback | None = None,
    ) -> DownloadResult:
        """Download a single YouTube video by its 11-character video ID.

        Raises DownloadError if video_id is invalid.
        """
        if not _YT_VIDEO_ID_RE.match(video_id):
            raise DownloadError(
                f"Invalid video_id {video_id!r}: must be 11 URL-safe base64 characters"
            )

        loop = asyncio.get_running_loop()
        url = f"https://www.youtube.com/watch?v={video_id}"
        return await self._download_one(
            url=url,
            yt_id=video_id,
            cache_id=None,
            noplaylist=True,
            progress_callback=progress_callback,
            loop=loop,
        )

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
            info = await asyncio.to_thread(self._run_ydl, ydl_opts, url)
        except VideoUnavailableError:
            self._cleanup_partials(yt_id)
            raise

        return self._build_result(info, cache_id=cache_id)

    async def _download_playlist(
        self,
        url: str,
        max_tracks: int,
        progress_callback: ProgressCallback | None,
        loop: asyncio.AbstractEventLoop,
    ) -> list[DownloadResult]:
        """Run yt-dlp for a playlist URL and return a DownloadResult per entry."""
        ydl_opts = self._build_opts(
            noplaylist=False,
            progress_callback=progress_callback,
            loop=loop,
            playlistend=max_tracks,
        )

        info = await asyncio.to_thread(self._run_ydl, ydl_opts, url)

        entries = info.get("entries") or []
        results: list[DownloadResult] = []
        for entry in entries:
            results.append(self._build_result(entry))
        return results

    # ------------------------------------------------------------------
    # yt-dlp orchestration (synchronous — runs inside to_thread)
    # ------------------------------------------------------------------

    def _run_ydl(self, ydl_opts: dict[str, Any], url: str) -> dict[str, Any]:
        """Call yt-dlp synchronously; translate errors to our hierarchy."""
        # Copy cookies to a temp file so yt-dlp never overwrites the original
        opts = ydl_opts
        tmp_path: str | None = None
        if self._cookies_file is not None:
            fd, tmp_path = tempfile.mkstemp(suffix=".txt")
            os.close(fd)
            shutil.copy2(self._cookies_file, tmp_path)
            opts = {**ydl_opts, "cookiefile": tmp_path}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info: dict[str, Any] = ydl.extract_info(url, download=True)
            return info
        except (
            yt_dlp.utils.DownloadError,
            yt_dlp.utils.ExtractorError,
            yt_dlp.utils.UnsupportedError,
        ) as exc:
            raise VideoUnavailableError(str(exc)) from exc
        except Exception as exc:
            raise DownloadError(f"Unexpected download error: {exc}") from exc
        finally:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)

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
        file_size = file_path.stat().st_size

        if file_size > self._max_file_size_bytes:
            raise FileTooLargeError(file_size, self._max_file_size_bytes)

        # The result's video_id is the cache key: caller override or yt-dlp ID
        result_id = cache_id if cache_id is not None else ydl_id

        artist = info.get("uploader") or info.get("channel") or None

        return DownloadResult(
            file_path=file_path,
            video_id=result_id,
            title=info["title"],
            artist=artist,
            duration_seconds=info.get("duration"),
            thumbnail_url=info.get("thumbnail"),
            file_size_bytes=file_size,
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
                # 1. Convert WebP thumbnail to JPG first (must precede embed)
                {"key": "FFmpegThumbnailsConvertor", "format": "jpg"},
                # 2. Embed the JPG thumbnail into the audio container
                {"key": "EmbedThumbnail"},
                # 3. Write all metadata tags
                {"key": "FFmpegMetadata"},
            ],
            "quiet": True,
            "no_warnings": True,
            "noplaylist": noplaylist,
            "progress_hooks": [self._make_sync_progress_hook(progress_callback, loop)],
            # Explicitly enable Node.js for YouTube JS signature challenges
            "js_runtimes": {"node": {}},
            # Point bgutil HTTP provider at the sidecar container.
            # Default is 127.0.0.1:4416 — must override with the Docker service name.
            "extractor_args": {
                "youtubepot-bgutilhttp": {
                    "base_url": ["http://bgutil:4416"],
                }
            },
        }
        if playlistend is not None:
            opts["playlistend"] = playlistend
        if self._cookies_file is not None:
            opts["cookiefile"] = str(self._cookies_file)
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

    def _cleanup_partials(self, yt_id: str | None) -> None:
        """Remove any partial files for *yt_id* from download_dir."""
        if not yt_id:
            return
        for path in self._download_dir.glob(f"{yt_id}.*"):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
