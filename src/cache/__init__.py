"""Cache layer: CompositeCache and create_cache factory."""

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

    async def get(self, video_id: str) -> Path | None:
        # L1: disk
        path = await self.disk.get(video_id)
        if path is not None:
            return path

        # L2: S3
        path = await self.s3.get(video_id)
        if path is not None:
            # Backfill disk
            try:
                path = await self.disk.put(video_id, path)
            except Exception as exc:
                logger.warning(
                    "CompositeCache: disk backfill failed for %s: %s", video_id, exc
                )
        return path

    async def put(self, video_id: str, file_path: Path) -> Path:
        # Always write to disk first — this is our authoritative return value.
        disk_path = await self.disk.put(video_id, file_path)

        # Best-effort S3 upload — failure must not crash the bot.
        try:
            await self.s3.put(video_id, file_path)
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

    assert settings.S3_BUCKET is not None  # validated in Settings.validate_s3_config
    s3 = S3Cache(
        bucket=settings.S3_BUCKET,
        region=settings.AWS_REGION,
        local_tmp_dir=settings.CACHE_DIR / "s3_tmp",
    )
    return CompositeCache(disk=disk, s3=s3)
