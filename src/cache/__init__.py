"""Cache layer: CompositeCache and create_cache factory."""

import asyncio
import logging
from pathlib import Path

from src.cache.base import CacheBackend
from src.cache.disk import DiskCache
from src.cache.s3 import S3Cache
from src.config import Settings

logger = logging.getLogger(__name__)

__all__ = ["CacheBackend", "CompositeCache", "DiskCache", "S3Cache", "create_cache"]


class CompositeCache(CacheBackend):
    """Two-tier cache: disk (L1) + S3 (L2).

    * get  — disk first; on miss fall back to S3 and backfill disk.
    * put  — write to disk AND S3 (S3 failure is non-fatal).
    * exists — disk first, then S3.
    * evict  — both layers.
    * total_size_bytes — sum of both layers.
    """

    def __init__(self, disk: DiskCache, s3: S3Cache) -> None:
        self.disk = disk
        self.s3 = s3
        # Per-video_id locks with reference counts to serialise concurrent
        # S3 → disk backfills.  The refcount tracks callers currently using
        # or waiting on a lock so entries are evicted only when the last
        # caller leaves — the same pattern used by AudioDownloader._inflight.
        # All dict mutations happen synchronously (no await between check and
        # write), so asyncio's single-threaded cooperative model keeps them
        # race-free without an additional guard lock.
        self._backfill_locks: dict[str, tuple[asyncio.Lock, int]] = {}

    async def get(self, video_id: str) -> Path | None:
        # L1: disk
        path = await self.disk.get(video_id)
        if path is not None:
            return path

        # L2: S3 — serialise backfill per video_id.
        # Register interest (bump refcount) before acquiring the lock so that
        # the entry cannot be evicted by a concurrent caller between our
        # lookup and our async with.
        if video_id in self._backfill_locks:
            lock, count = self._backfill_locks[video_id]
            self._backfill_locks[video_id] = (lock, count + 1)
        else:
            lock = asyncio.Lock()
            self._backfill_locks[video_id] = (lock, 1)

        try:
            async with lock:
                # Re-check disk: another coroutine may have backfilled while
                # we were waiting for the lock.
                path = await self.disk.get(video_id)
                if path is not None:
                    return path

                path = await self.s3.get(video_id)
                if path is not None:
                    try:
                        path = await self.disk.put(video_id, path)
                    except Exception as exc:
                        logger.warning(
                            "CompositeCache: disk backfill failed for %s: %s",
                            video_id,
                            exc,
                        )
        finally:
            # Decrement refcount; evict only when no other caller remains.
            entry = self._backfill_locks.get(video_id)
            if entry is not None and entry[0] is lock:
                _, count = entry
                if count <= 1:
                    self._backfill_locks.pop(video_id, None)
                else:
                    self._backfill_locks[video_id] = (lock, count - 1)

        return path

    async def put(self, video_id: str, file_path: Path) -> Path:
        # Always write to disk first — this is our authoritative return value.
        disk_path = await self.disk.put(video_id, file_path)

        # Best-effort S3 upload — failure must not crash the bot.
        # Use disk_path (not file_path) because disk.put may have renamed it.
        try:
            await self.s3.put(video_id, disk_path)
        except Exception as exc:
            logger.warning(
                "CompositeCache: S3 put failed for %s (non-fatal): %s", video_id, exc
            )

        return disk_path

    async def exists(self, video_id: str) -> bool:
        if await self.disk.exists(video_id):
            return True
        return await self.s3.exists(video_id)

    async def evict(self, video_id: str) -> None:
        await self.disk.evict(video_id)
        await self.s3.evict(video_id)

    async def total_size_bytes(self) -> int:
        disk_total = await self.disk.total_size_bytes()
        s3_total = await self.s3.total_size_bytes()
        return disk_total + s3_total

    async def get_file_id(self, video_id: str) -> str | None:
        """Delegate to disk cache (file_ids are local to this bot instance)."""
        return await self.disk.get_file_id(video_id)

    async def store_file_id(self, video_id: str, file_id: str) -> None:
        """Delegate to disk cache (file_ids are local to this bot instance)."""
        await self.disk.store_file_id(video_id, file_id)

    async def get_chapters(self, video_id: str) -> tuple[tuple[int, str], ...] | None:
        return await self.disk.get_chapters(video_id)

    async def store_chapters(
        self, video_id: str, chapters: tuple[tuple[int, str], ...]
    ) -> None:
        await self.disk.store_chapters(video_id, chapters)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_cache(settings: Settings) -> CacheBackend:
    """Return the appropriate CacheBackend based on *settings*.

    * ``S3_ENABLED=False``  →  :class:`DiskCache`
    * ``S3_ENABLED=True``   →  :class:`CompositeCache` (disk + S3)
    """
    disk = DiskCache(
        cache_dir=settings.CACHE_DIR,
        max_size_bytes=int(settings.CACHE_MAX_SIZE_GB * 1024**3),
    )

    if not settings.S3_ENABLED:
        return disk

    if settings.S3_BUCKET is None:
        raise RuntimeError("S3_BUCKET must be set when S3_ENABLED=True")
    s3_tmp_dir = settings.CACHE_DIR / "s3_tmp"
    s3_tmp_dir.mkdir(parents=True, exist_ok=True)
    s3 = S3Cache(
        bucket=settings.S3_BUCKET,
        region=settings.AWS_REGION,
        local_tmp_dir=s3_tmp_dir,
    )
    return CompositeCache(disk=disk, s3=s3)
