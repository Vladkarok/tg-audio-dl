"""Local filesystem LRU cache backend."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
import re
import time
from pathlib import Path

import aiofiles

from src.cache.base import CacheBackend, validate_video_id

CHUNK_SIZE = 256 * 1024  # 256 KB
# Telegram file_ids are Base64-encoded and may contain A-Z, a-z, 0-9, _, -, =, .
_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_\-.=]{1,512}$")

# Audio extensions supported by yt-dlp output, in priority order for discovery
_AUDIO_EXTENSIONS = (".m4a", ".opus", ".webm", ".mp3", ".ogg")
# Sidecar suffixes that are not audio files
_SIDECAR_SUFFIXES = frozenset((".fid", ".chapters.json"))

logger = logging.getLogger(__name__)


class DiskCache(CacheBackend):
    """File-system backed cache with LRU eviction.

    Files are stored as ``{cache_dir}/{video_id}.{ext}`` where ext is the
    actual audio format (m4a, opus, webm, etc.).
    Access time (atime) is used to track LRU order.
    """

    def __init__(self, cache_dir: Path, max_size_bytes: int) -> None:
        self.cache_dir = cache_dir
        self.max_size_bytes = max_size_bytes
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _locate_audio(self, video_id: str) -> Path | None:
        """Find an existing cached audio file for *video_id*, any extension."""
        for ext in _AUDIO_EXTENSIONS:
            candidate = self.cache_dir / f"{video_id}{ext}"
            if candidate.exists():
                return candidate
        return None

    def _fid_path(self, video_id: str) -> Path:
        return self.cache_dir / f"{video_id}.fid"

    def _chapters_path(self, video_id: str) -> Path:
        return self.cache_dir / f"{video_id}.chapters.json"

    def _ensure_cache_dir(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _list_audio_files(self) -> list[Path]:
        """Return all cached audio files (any supported extension)."""
        if not self.cache_dir.exists():
            return []
        result: list[Path] = []
        for ext in _AUDIO_EXTENSIONS:
            result.extend(self.cache_dir.glob(f"*{ext}"))
        return result

    # ------------------------------------------------------------------
    # CacheBackend interface
    # ------------------------------------------------------------------

    async def get(self, video_id: str) -> Path | None:
        """Return cached path and update access time, or None if missing."""
        validate_video_id(video_id)
        path = self._locate_audio(video_id)
        if path is None:
            return None
        # Update atime for LRU tracking
        os.utime(path, None)
        return path

    async def put(self, video_id: str, file_path: Path) -> Path:
        """Move or copy *file_path* into cache; trigger LRU eviction if needed.

        The returned path preserves the extension of *file_path*.
        """
        validate_video_id(video_id)
        self._ensure_cache_dir()
        dest = self.cache_dir / f"{video_id}{file_path.suffix}"

        # Try atomic rename first (same filesystem); fall back to chunked copy
        try:
            # Same-filesystem rename is a tiny metadata update, so doing it
            # inline keeps the fast path simple and avoids a threaded rename
            # hang seen in some sandboxed runtimes.
            os.rename(file_path, dest)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            try:
                async with (
                    aiofiles.open(file_path, "rb") as src_f,
                    aiofiles.open(dest, "wb") as dst_f,
                ):
                    while chunk := await src_f.read(CHUNK_SIZE):
                        await dst_f.write(chunk)
            except OSError:
                with contextlib.suppress(OSError):
                    dest.unlink()
                raise
            # Remove source to complete the cross-device "move"
            with contextlib.suppress(OSError):
                file_path.unlink()

        await self.evict_lru_if_needed()
        return dest

    async def exists(self, video_id: str) -> bool:
        validate_video_id(video_id)
        return self._locate_audio(video_id) is not None

    async def evict(self, video_id: str) -> None:
        validate_video_id(video_id)
        # Remove any audio file regardless of extension
        path = self._locate_audio(video_id)
        if path is not None:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        with contextlib.suppress(FileNotFoundError):
            self._fid_path(video_id).unlink()
        with contextlib.suppress(FileNotFoundError):
            self._chapters_path(video_id).unlink()

    async def get_file_id(self, video_id: str) -> str | None:
        """Return stored Telegram file_id or None if not stored."""
        validate_video_id(video_id)
        path = self._fid_path(video_id)
        if not path.exists():
            return None
        return path.read_text().strip()

    async def store_file_id(self, video_id: str, file_id: str) -> None:
        """Persist a Telegram file_id for the given video_id."""
        validate_video_id(video_id)
        if not _FILE_ID_RE.fullmatch(file_id):
            logger.warning("store_file_id: invalid file_id %r, skipping", file_id)
            return
        self._ensure_cache_dir()
        self._fid_path(video_id).write_text(file_id)

    async def get_chapters(self, video_id: str) -> tuple[tuple[int, str], ...] | None:
        """Return cached chapters from JSON sidecar, or None."""
        validate_video_id(video_id)
        path = self._chapters_path(video_id)
        if not path.exists():
            return None
        try:
            text = path.read_text()
            data = json.loads(text)
            return tuple((int(s), str(t)) for s, t in data)
        except Exception:
            logger.debug("Could not read chapters for %s", video_id, exc_info=True)
            return None

    async def store_chapters(
        self, video_id: str, chapters: tuple[tuple[int, str], ...]
    ) -> None:
        """Persist chapters as a JSON sidecar file."""
        validate_video_id(video_id)
        self._ensure_cache_dir()
        data = json.dumps(list(chapters))
        self._chapters_path(video_id).write_text(data)

    async def total_size_bytes(self) -> int:
        paths = self._list_audio_files()
        total = 0
        for p in paths:
            try:
                stat = p.stat()
                total += stat.st_size
            except FileNotFoundError:
                pass
        return total

    # ------------------------------------------------------------------
    # LRU eviction
    # ------------------------------------------------------------------

    async def evict_lru_if_needed(self) -> None:
        """Delete oldest-accessed files until total size is within budget."""
        async with self._lock:
            total = await self.total_size_bytes()
            if total <= self.max_size_bytes:
                return

            # Collect (atime, path) for all cached audio files
            paths = self._list_audio_files()
            stats: list[tuple[float, Path]] = []
            for p in paths:
                try:
                    stat = p.stat()
                    stats.append((stat.st_atime, p))
                except FileNotFoundError:
                    pass

            # Oldest first
            stats.sort(key=lambda x: x[0])

            for _atime, path in stats:
                if total <= self.max_size_bytes:
                    break
                try:
                    size = path.stat().st_size
                    path.unlink()
                    total -= size
                    logger.info("DiskCache: evicted %s (LRU)", path.name)
                    # Clean up orphaned sidecar files
                    video_id = path.stem
                    for suf in _SIDECAR_SUFFIXES:
                        sidecar = self.cache_dir / f"{video_id}{suf}"
                        with contextlib.suppress(FileNotFoundError):
                            sidecar.unlink()
                except FileNotFoundError:
                    pass


# ---------------------------------------------------------------------------
# Stale tmp cleanup
# ---------------------------------------------------------------------------


async def cleanup_stale_tmp(tmp_dir: Path, max_age_seconds: float) -> int:
    """Delete files in *tmp_dir* older than *max_age_seconds*. Returns count."""
    if not tmp_dir.exists():
        return 0
    now = time.time()
    deleted = 0
    for entry in tmp_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            mtime = entry.stat().st_mtime
            if now - mtime > max_age_seconds:
                entry.unlink()
                deleted += 1
                logger.debug("cleanup_stale_tmp: removed %s", entry.name)
        except FileNotFoundError:
            pass
    if deleted:
        logger.info("cleanup_stale_tmp: removed %d stale file(s)", deleted)
    return deleted
