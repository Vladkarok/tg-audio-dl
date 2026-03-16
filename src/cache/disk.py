"""Local filesystem LRU cache backend."""

import asyncio
import contextlib
import logging
import os
import re
from pathlib import Path

import aiofiles

from src.cache.base import CacheBackend

CHUNK_SIZE = 256 * 1024  # 256 KB

logger = logging.getLogger(__name__)

# YouTube 11-char IDs, SoundCloud numeric IDs, and sc_slug cache keys (up to 64 chars).
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _validate_video_id(video_id: str) -> None:
    """Raise ValueError if video_id does not match the expected pattern."""
    if not _VIDEO_ID_RE.match(video_id):
        raise ValueError(
            f"Invalid video_id {video_id!r}: must match ^[A-Za-z0-9_-]{{1,64}}$"
        )


class DiskCache(CacheBackend):
    """File-system backed cache with LRU eviction.

    Files are stored as ``{cache_dir}/{video_id}.m4a``.
    Access time (atime) is used to track LRU order.
    """

    def __init__(self, cache_dir: Path, max_size_bytes: int) -> None:
        self.cache_dir = cache_dir
        self.max_size_bytes = max_size_bytes
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _path_for(self, video_id: str) -> Path:
        return self.cache_dir / f"{video_id}.m4a"

    def _fid_path(self, video_id: str) -> Path:
        return self.cache_dir / f"{video_id}.fid"

    def _ensure_cache_dir(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # CacheBackend interface
    # ------------------------------------------------------------------

    async def get(self, video_id: str) -> Path | None:
        """Return cached path and update access time, or None if missing."""
        _validate_video_id(video_id)
        path = self._path_for(video_id)
        if not path.exists():
            return None
        # Update atime for LRU tracking
        await asyncio.to_thread(os.utime, path, None)
        return path

    async def put(self, video_id: str, file_path: Path) -> Path:
        """Copy *file_path* into the cache and trigger LRU eviction if needed."""
        _validate_video_id(video_id)
        self._ensure_cache_dir()
        dest = self._path_for(video_id)

        # Async copy via aiofiles in chunks to avoid loading entire file into RAM
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

        await self.evict_lru_if_needed()
        return dest

    async def exists(self, video_id: str) -> bool:
        _validate_video_id(video_id)
        return self._path_for(video_id).exists()

    async def evict(self, video_id: str) -> None:
        _validate_video_id(video_id)
        path = self._path_for(video_id)
        with contextlib.suppress(FileNotFoundError):
            await asyncio.to_thread(path.unlink)
        with contextlib.suppress(FileNotFoundError):
            await asyncio.to_thread(self._fid_path(video_id).unlink)

    async def get_file_id(self, video_id: str) -> str | None:
        """Return stored Telegram file_id or None if not stored."""
        _validate_video_id(video_id)
        path = self._fid_path(video_id)
        if not await asyncio.to_thread(path.exists):
            return None
        return (await asyncio.to_thread(path.read_text)).strip()

    async def store_file_id(self, video_id: str, file_id: str) -> None:
        """Persist a Telegram file_id for the given video_id."""
        _validate_video_id(video_id)
        self._ensure_cache_dir()
        await asyncio.to_thread(self._fid_path(video_id).write_text, file_id)

    async def total_size_bytes(self) -> int:
        if not self.cache_dir.exists():
            return 0
        paths = list(self.cache_dir.glob("*.m4a"))
        total = 0
        for p in paths:
            try:
                stat = await asyncio.to_thread(p.stat)
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

            # Collect (atime, path) for all cached files
            paths = list(self.cache_dir.glob("*.m4a"))
            stats: list[tuple[float, Path]] = []
            for p in paths:
                try:
                    stat = await asyncio.to_thread(p.stat)
                    stats.append((stat.st_atime, p))
                except FileNotFoundError:
                    pass

            # Oldest first
            stats.sort(key=lambda x: x[0])

            for _atime, path in stats:
                if total <= self.max_size_bytes:
                    break
                try:
                    size = (await asyncio.to_thread(path.stat)).st_size
                    await asyncio.to_thread(path.unlink)
                    total -= size
                    logger.info("DiskCache: evicted %s (LRU)", path.name)
                except FileNotFoundError:
                    pass
