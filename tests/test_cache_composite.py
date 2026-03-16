"""Tests for src/cache/__init__.py — CompositeCache + create_cache factory.

Phase 3.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.cache import CompositeCache, create_cache
from src.cache.disk import DiskCache
from src.cache.s3 import S3Cache


@pytest.fixture
def source_file(tmp_path: Path) -> Path:
    f = tmp_path / "source.m4a"
    f.write_bytes(b"composite_audio_data")
    return f


@pytest.fixture
def cached_disk_path(tmp_path: Path) -> Path:
    """A file that simulates an already-cached disk file."""
    p = tmp_path / "disk_cached.m4a"
    p.write_bytes(b"disk_cached_data")
    return p


def _make_mock_disk(
    *,
    get_return=None,
    exists_return=False,
    put_return=None,
) -> AsyncMock:
    disk = AsyncMock(spec=DiskCache)
    disk.get.return_value = get_return
    disk.exists.return_value = exists_return
    disk.put.return_value = put_return
    return disk


def _make_mock_s3(
    *,
    get_return=None,
    exists_return=False,
    put_return=None,
) -> AsyncMock:
    s3 = AsyncMock(spec=S3Cache)
    s3.get.return_value = get_return
    s3.exists.return_value = exists_return
    s3.put.return_value = put_return
    return s3


# ---------------------------------------------------------------------------
# get()
# ---------------------------------------------------------------------------


class TestCompositeCacheGet:
    async def test_composite_get_disk_hit(self, cached_disk_path: Path) -> None:
        """Disk hit: returns disk path immediately, S3 never queried."""
        disk = _make_mock_disk(get_return=cached_disk_path)
        s3 = _make_mock_s3()
        cache = CompositeCache(disk=disk, s3=s3)

        result = await cache.get("dQw4w9WgXcQ")

        assert result == cached_disk_path
        disk.get.assert_called_once_with("dQw4w9WgXcQ")
        s3.get.assert_not_called()

    async def test_composite_get_s3_fallback(
        self, tmp_path: Path, cached_disk_path: Path
    ) -> None:
        """Disk miss → S3 hit → backfills disk → returns S3-downloaded path."""
        s3_downloaded = tmp_path / "s3_download.m4a"
        s3_downloaded.write_bytes(b"s3_data")

        disk = _make_mock_disk(get_return=None, put_return=cached_disk_path)
        s3 = _make_mock_s3(get_return=s3_downloaded)
        cache = CompositeCache(disk=disk, s3=s3)

        result = await cache.get("dQw4w9WgXcQ")

        assert result is not None
        s3.get.assert_called_once_with("dQw4w9WgXcQ")
        # disk.put should be called to backfill
        disk.put.assert_called_once_with("dQw4w9WgXcQ", s3_downloaded)

    async def test_composite_get_full_miss(self) -> None:
        """Both disk and S3 miss → returns None."""
        disk = _make_mock_disk(get_return=None)
        s3 = _make_mock_s3(get_return=None)
        cache = CompositeCache(disk=disk, s3=s3)

        result = await cache.get("dQw4w9WgXcQ")

        assert result is None


# ---------------------------------------------------------------------------
# put()
# ---------------------------------------------------------------------------


class TestCompositeCachePut:
    async def test_composite_put_writes_both(
        self, source_file: Path, cached_disk_path: Path
    ) -> None:
        disk = _make_mock_disk(put_return=cached_disk_path)
        s3 = _make_mock_s3(put_return=source_file)
        cache = CompositeCache(disk=disk, s3=s3)

        result = await cache.put("dQw4w9WgXcQ", source_file)

        disk.put.assert_called_once_with("dQw4w9WgXcQ", source_file)
        s3.put.assert_called_once_with("dQw4w9WgXcQ", source_file)
        # Returns the disk path
        assert result == cached_disk_path

    async def test_composite_put_s3_failure_nonfatal(
        self, source_file: Path, cached_disk_path: Path
    ) -> None:
        """S3 put raises → disk write still succeeds, no exception propagated."""
        disk = _make_mock_disk(put_return=cached_disk_path)
        s3 = _make_mock_s3()
        s3.put.side_effect = Exception("S3 is down")
        cache = CompositeCache(disk=disk, s3=s3)

        result = await cache.put("dQw4w9WgXcQ", source_file)

        disk.put.assert_called_once()
        assert result == cached_disk_path


# ---------------------------------------------------------------------------
# exists()
# ---------------------------------------------------------------------------


class TestCompositeCacheExists:
    async def test_composite_exists_disk_first_true(self) -> None:
        disk = _make_mock_disk(exists_return=True)
        s3 = _make_mock_s3(exists_return=False)
        cache = CompositeCache(disk=disk, s3=s3)

        assert await cache.exists("dQw4w9WgXcQ") is True
        disk.exists.assert_called_once_with("dQw4w9WgXcQ")
        s3.exists.assert_not_called()

    async def test_composite_exists_s3_fallback(self) -> None:
        disk = _make_mock_disk(exists_return=False)
        s3 = _make_mock_s3(exists_return=True)
        cache = CompositeCache(disk=disk, s3=s3)

        assert await cache.exists("dQw4w9WgXcQ") is True
        s3.exists.assert_called_once_with("dQw4w9WgXcQ")

    async def test_composite_exists_both_miss(self) -> None:
        disk = _make_mock_disk(exists_return=False)
        s3 = _make_mock_s3(exists_return=False)
        cache = CompositeCache(disk=disk, s3=s3)

        assert await cache.exists("dQw4w9WgXcQ") is False


# ---------------------------------------------------------------------------
# evict()
# ---------------------------------------------------------------------------


class TestCompositeCacheEvict:
    async def test_composite_evict_calls_both(self) -> None:
        disk = _make_mock_disk()
        s3 = _make_mock_s3()
        cache = CompositeCache(disk=disk, s3=s3)

        await cache.evict("dQw4w9WgXcQ")

        disk.evict.assert_called_once_with("dQw4w9WgXcQ")
        s3.evict.assert_called_once_with("dQw4w9WgXcQ")


# ---------------------------------------------------------------------------
# total_size_bytes()
# ---------------------------------------------------------------------------


class TestCompositeCacheTotalSize:
    async def test_composite_total_size_sums_both(self) -> None:
        disk = _make_mock_disk()
        disk.total_size_bytes = AsyncMock(return_value=500)
        s3 = _make_mock_s3()
        s3.total_size_bytes = AsyncMock(return_value=1000)
        cache = CompositeCache(disk=disk, s3=s3)

        total = await cache.total_size_bytes()
        assert total == 1500


# ---------------------------------------------------------------------------
# create_cache() factory
# ---------------------------------------------------------------------------


class TestCreateCacheFactory:
    def _make_settings(self, *, s3_enabled: bool, s3_bucket: str | None = None):
        settings = MagicMock()
        settings.S3_ENABLED = s3_enabled
        settings.S3_BUCKET = s3_bucket or "my-bucket"
        settings.AWS_REGION = "us-east-1"
        settings.CACHE_DIR = Path("/nonexistent/placeholder")  # overridden by tests
        settings.CACHE_MAX_SIZE_GB = 5.0
        return settings

    def test_composite_factory_disk_only(self, tmp_path: Path) -> None:
        settings = self._make_settings(s3_enabled=False)
        settings.CACHE_DIR = tmp_path / "cache"
        cache = create_cache(settings)
        assert isinstance(cache, DiskCache)

    def test_composite_factory_composite(self, tmp_path: Path) -> None:
        settings = self._make_settings(s3_enabled=True, s3_bucket="my-bucket")
        settings.CACHE_DIR = tmp_path / "cache"
        cache = create_cache(settings)
        assert isinstance(cache, CompositeCache)


# ---------------------------------------------------------------------------
# file_id delegation
# ---------------------------------------------------------------------------


class TestCompositeCacheFileId:
    async def test_get_file_id_delegates_to_disk(self) -> None:
        """CompositeCache.get_file_id delegates to disk cache."""
        disk = _make_mock_disk()
        disk.get_file_id = AsyncMock(return_value="AgACtest_id")
        s3 = _make_mock_s3()
        cache = CompositeCache(disk=disk, s3=s3)

        result = await cache.get_file_id("dQw4w9WgXcQ")
        assert result == "AgACtest_id"
        disk.get_file_id.assert_called_once_with("dQw4w9WgXcQ")

    async def test_store_file_id_delegates_to_disk(self) -> None:
        """CompositeCache.store_file_id delegates to disk cache."""
        disk = _make_mock_disk()
        disk.store_file_id = AsyncMock()
        s3 = _make_mock_s3()
        cache = CompositeCache(disk=disk, s3=s3)

        await cache.store_file_id("dQw4w9WgXcQ", "AgACtest_id")
        disk.store_file_id.assert_called_once_with("dQw4w9WgXcQ", "AgACtest_id")
