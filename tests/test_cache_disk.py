"""Tests for src/cache/disk.py — Phase 3."""

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from src.cache.disk import DiskCache


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    return tmp_path / "cache"


@pytest.fixture
def source_file(tmp_path: Path) -> Path:
    """A small fake .m4a source file to put into cache."""
    f = tmp_path / "source.m4a"
    f.write_bytes(b"audio_data_1234")
    return f


@pytest.fixture
def disk_cache(cache_dir: Path) -> DiskCache:
    return DiskCache(cache_dir=cache_dir, max_size_bytes=10 * 1024 * 1024)


# ---------------------------------------------------------------------------
# put / get / exists / evict
# ---------------------------------------------------------------------------


class TestDiskCachePutGetExistsEvict:
    async def test_disk_put_creates_file(
        self, disk_cache: DiskCache, source_file: Path
    ) -> None:
        result = await disk_cache.put("dQw4w9WgXcQ", source_file)
        assert result.exists()
        assert result.parent == disk_cache.cache_dir
        assert result.name == "dQw4w9WgXcQ.m4a"

    async def test_disk_get_returns_path(
        self, disk_cache: DiskCache, source_file: Path
    ) -> None:
        await disk_cache.put("dQw4w9WgXcQ", source_file)
        result = await disk_cache.get("dQw4w9WgXcQ")
        assert result is not None
        assert result.exists()

    async def test_disk_get_returns_none_for_missing(
        self, disk_cache: DiskCache
    ) -> None:
        result = await disk_cache.get("missingVidX")
        assert result is None

    async def test_disk_exists_true(
        self, disk_cache: DiskCache, source_file: Path
    ) -> None:
        await disk_cache.put("dQw4w9WgXcQ", source_file)
        assert await disk_cache.exists("dQw4w9WgXcQ") is True

    async def test_disk_exists_false(self, disk_cache: DiskCache) -> None:
        assert await disk_cache.exists("missingVidX") is False

    async def test_disk_evict_removes_file(
        self, disk_cache: DiskCache, source_file: Path
    ) -> None:
        await disk_cache.put("dQw4w9WgXcQ", source_file)
        await disk_cache.evict("dQw4w9WgXcQ")
        assert await disk_cache.exists("dQw4w9WgXcQ") is False

    async def test_disk_evict_nonexistent_is_safe(self, disk_cache: DiskCache) -> None:
        # Must not raise
        await disk_cache.evict("missingVidX")

    async def test_disk_total_size(self, disk_cache: DiskCache, tmp_path: Path) -> None:
        f1 = tmp_path / "a.m4a"
        f2 = tmp_path / "b.m4a"
        f1.write_bytes(b"A" * 100)
        f2.write_bytes(b"B" * 200)
        await disk_cache.put("AAAAAAAAAAA", f1)
        await disk_cache.put("BBBBBBBBBBB", f2)
        total = await disk_cache.total_size_bytes()
        assert total == 300

    async def test_disk_total_size_empty_dir(self, tmp_path: Path) -> None:
        """total_size_bytes() returns 0 when cache directory does not exist."""
        cache = DiskCache(cache_dir=tmp_path / "nonexistent_cache", max_size_bytes=1024)
        assert await cache.total_size_bytes() == 0


# ---------------------------------------------------------------------------
# Directory creation
# ---------------------------------------------------------------------------


class TestDiskCacheDirCreation:
    async def test_disk_creates_cache_dir_if_missing(self, tmp_path: Path) -> None:
        new_dir = tmp_path / "deep" / "nested" / "cache"
        assert not new_dir.exists()
        cache = DiskCache(cache_dir=new_dir, max_size_bytes=1024)
        source = tmp_path / "src.m4a"
        source.write_bytes(b"x")
        await cache.put("dQw4w9WgXcQ", source)
        assert new_dir.exists()


# ---------------------------------------------------------------------------
# LRU eviction
# ---------------------------------------------------------------------------


class TestDiskCacheLRUEviction:
    async def test_disk_lru_eviction_on_overflow(self, tmp_path: Path) -> None:
        """Put three 100-byte files into a 250-byte cache.
        The oldest-accessed file should be evicted to make room."""
        cache_dir = tmp_path / "lru_cache"
        cache = DiskCache(cache_dir=cache_dir, max_size_bytes=250)

        files = []
        for _i, vid in enumerate(["AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC"]):
            f = tmp_path / f"{vid}.m4a"
            f.write_bytes(b"X" * 100)
            files.append((vid, f))

        # Put first two — total 200 bytes, within limit
        await cache.put(files[0][0], files[0][1])
        # Small sleep so access times differ even on fast filesystems
        await asyncio.sleep(0.01)
        await cache.put(files[1][0], files[1][1])
        await asyncio.sleep(0.01)

        # Access first file to make it more-recently-used than the second
        await cache.get(files[0][0])
        await asyncio.sleep(0.01)

        # Put third file — total would be 300 > 250, so LRU (second) must go
        await cache.put(files[2][0], files[2][1])

        remaining = list(cache_dir.glob("*.m4a"))
        remaining_names = {p.stem for p in remaining}

        assert "BBBBBBBBBBB" not in remaining_names, (
            "LRU file (BBBBBBBBBBB) should have been evicted"
        )
        assert "AAAAAAAAAAA" in remaining_names
        assert "CCCCCCCCCCC" in remaining_names

    async def test_disk_get_updates_access_time(
        self, disk_cache: DiskCache, source_file: Path
    ) -> None:
        """get() must update the file's access time so LRU ordering works."""
        await disk_cache.put("dQw4w9WgXcQ", source_file)
        cached_path = disk_cache.cache_dir / "dQw4w9WgXcQ.m4a"

        before = cached_path.stat().st_atime
        await asyncio.sleep(0.05)
        await disk_cache.get("dQw4w9WgXcQ")
        after = cached_path.stat().st_atime

        assert after >= before, "access time should be updated after get()"


# ---------------------------------------------------------------------------
# Security: video_id validation
# ---------------------------------------------------------------------------


class TestDiskCacheVideoIdValidation:
    @pytest.mark.parametrize(
        "bad_id",
        [
            "../etc/passwd",
            "../../secret",
            "has space   ",
            "has/slash!!",
            "",
            "a" * 65,  # exceeds 64-char limit
        ],
    )
    async def test_put_rejects_invalid_video_id(
        self, disk_cache: DiskCache, source_file: Path, bad_id: str
    ) -> None:
        with pytest.raises(ValueError, match="Invalid video_id"):
            await disk_cache.put(bad_id, source_file)

    @pytest.mark.parametrize(
        "bad_id",
        [
            "../etc/passwd",
            "has/slash!!",
            "a" * 65,
        ],
    )
    async def test_get_rejects_invalid_video_id(
        self, disk_cache: DiskCache, bad_id: str
    ) -> None:
        with pytest.raises(ValueError, match="Invalid video_id"):
            await disk_cache.get(bad_id)

    @pytest.mark.parametrize(
        "valid_id",
        [
            "dQw4w9WgXcQ",  # YouTube 11-char ID
            "sc_artist_track-name",  # SoundCloud slug
            "a" * 64,  # max length
        ],
    )
    async def test_put_accepts_valid_video_id(
        self, disk_cache: DiskCache, source_file: Path, valid_id: str
    ) -> None:
        result = await disk_cache.put(valid_id, source_file)
        assert result.exists()


# ---------------------------------------------------------------------------
# Race-condition defensive branches
# ---------------------------------------------------------------------------


def _place_file_directly(cache: DiskCache, video_id: str, data: bytes) -> Path:
    """Write a file directly into cache dir without triggering put/eviction."""
    cache.cache_dir.mkdir(parents=True, exist_ok=True)
    dest = cache.cache_dir / f"{video_id}.m4a"
    dest.write_bytes(data)
    return dest


class TestDiskCacheFileId:
    async def test_get_file_id_returns_none_when_not_stored(
        self, tmp_path: Path
    ) -> None:
        cache = DiskCache(tmp_path, max_size_bytes=10**9)
        result = await cache.get_file_id("dQw4w9WgXcQ")
        assert result is None

    async def test_store_and_get_file_id(self, tmp_path: Path) -> None:
        cache = DiskCache(tmp_path, max_size_bytes=10**9)
        await cache.store_file_id("dQw4w9WgXcQ", "AgACAgIA_test_file_id")
        result = await cache.get_file_id("dQw4w9WgXcQ")
        assert result == "AgACAgIA_test_file_id"

    async def test_evict_removes_fid_file(self, tmp_path: Path) -> None:
        cache = DiskCache(tmp_path, max_size_bytes=10**9)
        # Create fake .m4a and .fid
        (tmp_path / "dQw4w9WgXcQ.m4a").write_bytes(b"fake")
        await cache.store_file_id("dQw4w9WgXcQ", "some_file_id")
        await cache.evict("dQw4w9WgXcQ")
        assert await cache.get_file_id("dQw4w9WgXcQ") is None

    async def test_get_file_id_rejects_invalid_video_id(self, tmp_path: Path) -> None:
        cache = DiskCache(tmp_path, max_size_bytes=10**9)
        with pytest.raises(ValueError):
            await cache.get_file_id("../evil")

    async def test_store_file_id_rejects_invalid_video_id(self, tmp_path: Path) -> None:
        cache = DiskCache(tmp_path, max_size_bytes=10**9)
        with pytest.raises(ValueError):
            await cache.store_file_id("../evil", "some_id")

    async def test_total_size_excludes_fid_files(self, tmp_path: Path) -> None:
        cache = DiskCache(tmp_path, max_size_bytes=10**9)
        (tmp_path / "dQw4w9WgXcQ.m4a").write_bytes(b"x" * 100)
        await cache.store_file_id("dQw4w9WgXcQ", "some_id")
        size = await cache.total_size_bytes()
        assert size == 100  # .fid not counted


class TestDiskCacheRaceConditions:
    async def test_total_size_tolerates_vanished_file(self, tmp_path: Path) -> None:
        """total_size_bytes() silently ignores a file that disappears mid-scan
        (covers lines 103-104)."""
        cache = DiskCache(cache_dir=tmp_path / "race_cache", max_size_bytes=1024)
        _place_file_directly(cache, "dQw4w9WgXcQ", b"X" * 50)

        original_to_thread = asyncio.to_thread
        call_count = 0

        async def _patched(fn, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise FileNotFoundError("vanished during total_size scan")
            return await original_to_thread(fn, *args, **kwargs)

        with patch("src.cache.disk.asyncio.to_thread", _patched):
            total = await cache.total_size_bytes()

        assert total == 0

    async def test_evict_lru_tolerates_vanished_file_during_stat(
        self, tmp_path: Path
    ) -> None:
        """evict_lru_if_needed() skips files that vanish during stat collection
        (covers lines 125-126: FileNotFoundError in the LRU gather-stats loop).

        Call order when one oversize file is present:
          1: stat in total_size_bytes  → succeeds (total > limit)
          2: stat in LRU gather loop   → raise FileNotFoundError (line 125-126)
        """
        cache = DiskCache(cache_dir=tmp_path / "lru_race1", max_size_bytes=50)
        _place_file_directly(cache, "dQw4w9WgXcQ", b"X" * 100)

        original_to_thread = asyncio.to_thread
        call_count = 0

        async def _patched(fn, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise FileNotFoundError("gone during LRU stat collection")
            return await original_to_thread(fn, *args, **kwargs)

        with patch("src.cache.disk.asyncio.to_thread", _patched):
            await cache.evict_lru_if_needed()  # must not raise

    async def test_evict_lru_tolerates_vanished_file_during_unlink(
        self, tmp_path: Path
    ) -> None:
        """evict_lru_if_needed() skips files that vanish just before unlink
        (covers lines 139-140: FileNotFoundError in the eviction inner loop).

        Call order when one oversize file is present:
          1: stat in total_size_bytes  → succeeds (total > limit)
          2: stat in LRU gather loop   → succeeds (file added to stats list)
          3: stat before unlink        → raise FileNotFoundError (line 139-140)
        """
        cache = DiskCache(cache_dir=tmp_path / "lru_race2", max_size_bytes=50)
        _place_file_directly(cache, "dQw4w9WgXcQ", b"X" * 100)

        original_to_thread = asyncio.to_thread
        call_count = 0

        async def _patched(fn, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                raise FileNotFoundError("gone just before unlink")
            return await original_to_thread(fn, *args, **kwargs)

        with patch("src.cache.disk.asyncio.to_thread", _patched):
            await cache.evict_lru_if_needed()  # must not raise
