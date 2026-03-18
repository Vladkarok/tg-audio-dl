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
        (covers the FileNotFoundError branch in total_size_bytes)."""
        cache = DiskCache(cache_dir=tmp_path / "race_cache", max_size_bytes=1024)
        _place_file_directly(cache, "dQw4w9WgXcQ", b"X" * 50)
        target_name = "dQw4w9WgXcQ.m4a"

        path_type = type(cache.cache_dir / "dQw4w9WgXcQ.m4a")
        original_stat = path_type.stat
        call_count = 0

        def _patched(self, *args, **kwargs):
            nonlocal call_count
            if self.name == target_name:
                call_count += 1
            if self.name == target_name and call_count == 1:
                raise FileNotFoundError("vanished during total_size scan")
            return original_stat(self, *args, **kwargs)

        with patch.object(path_type, "stat", _patched):
            total = await cache.total_size_bytes()

        assert total == 0

    async def test_evict_lru_tolerates_vanished_file_during_stat(
        self, tmp_path: Path
    ) -> None:
        """evict_lru_if_needed() skips files that vanish during stat collection
        (covers FileNotFoundError in the LRU gather-stats loop).

        Call order when one oversize file is present:
          1: stat in total_size_bytes  → succeeds (total > limit)
          2: stat in LRU gather loop   → raise FileNotFoundError
        """
        cache = DiskCache(cache_dir=tmp_path / "lru_race1", max_size_bytes=50)
        _place_file_directly(cache, "dQw4w9WgXcQ", b"X" * 100)
        target_name = "dQw4w9WgXcQ.m4a"

        path_type = type(cache.cache_dir / "dQw4w9WgXcQ.m4a")
        original_stat = path_type.stat
        call_count = 0

        def _patched(self, *args, **kwargs):
            nonlocal call_count
            if self.name == target_name:
                call_count += 1
            if self.name == target_name and call_count == 2:
                raise FileNotFoundError("gone during LRU stat collection")
            return original_stat(self, *args, **kwargs)

        with patch.object(path_type, "stat", _patched):
            await cache.evict_lru_if_needed()  # must not raise

    async def test_evict_lru_tolerates_vanished_file_during_unlink(
        self, tmp_path: Path
    ) -> None:
        """evict_lru_if_needed() skips files that vanish just before unlink
        (covers FileNotFoundError in the eviction inner loop).

        Call order when one oversize file is present:
          1: stat in total_size_bytes  → succeeds (total > limit)
          2: stat in LRU gather loop   → succeeds (file added to stats list)
          3: stat before unlink        → raise FileNotFoundError
        """
        cache = DiskCache(cache_dir=tmp_path / "lru_race2", max_size_bytes=50)
        _place_file_directly(cache, "dQw4w9WgXcQ", b"X" * 100)
        target_name = "dQw4w9WgXcQ.m4a"

        path_type = type(cache.cache_dir / "dQw4w9WgXcQ.m4a")
        original_stat = path_type.stat
        call_count = 0

        def _patched(self, *args, **kwargs):
            nonlocal call_count
            if self.name == target_name:
                call_count += 1
            if self.name == target_name and call_count == 3:
                raise FileNotFoundError("gone just before unlink")
            return original_stat(self, *args, **kwargs)

        with patch.object(path_type, "stat", _patched):
            await cache.evict_lru_if_needed()  # must not raise


class TestLruEvictsFidSidecar:
    async def test_lru_eviction_removes_fid_sidecar(self, tmp_path):
        """When LRU evicts a .m4a, the corresponding .fid sidecar is also removed."""
        cache = DiskCache(cache_dir=tmp_path / "fid_cleanup", max_size_bytes=50)
        _place_file_directly(cache, "vid1", b"X" * 100)
        # Create a .fid sidecar
        fid_path = cache.cache_dir / "vid1.fid"
        fid_path.write_text("AgACAgIA_test_fid")

        await cache.evict_lru_if_needed()

        assert not (cache.cache_dir / "vid1.m4a").exists()
        assert not fid_path.exists()


class TestPutUsesRename:
    async def test_put_moves_file_on_same_fs(self, tmp_path):
        """put() should use rename (move) when source and dest are same filesystem."""
        cache = DiskCache(cache_dir=tmp_path / "cache", max_size_bytes=10**9)
        src_file = tmp_path / "source.m4a"
        src_file.write_bytes(b"audio data")

        result = await cache.put("testvid123", src_file)

        assert result == cache.cache_dir / "testvid123.m4a"
        assert result.read_bytes() == b"audio data"
        # Source should be gone (renamed, not copied)
        assert not src_file.exists()


class TestStoreFileIdValidation:
    async def test_valid_file_id_stored(self, tmp_path):
        cache = DiskCache(cache_dir=tmp_path / "fid", max_size_bytes=10**9)
        cache._ensure_cache_dir()
        await cache.store_file_id("vid1", "AgACAgIA_valid_id")
        assert await cache.get_file_id("vid1") == "AgACAgIA_valid_id"

    async def test_invalid_file_id_rejected(self, tmp_path):
        cache = DiskCache(cache_dir=tmp_path / "fid", max_size_bytes=10**9)
        cache._ensure_cache_dir()
        await cache.store_file_id("vid1", "../../etc/passwd")
        # Should not be stored
        assert await cache.get_file_id("vid1") is None


# ---------------------------------------------------------------------------
# Multi-format extension preservation
# ---------------------------------------------------------------------------


class TestDiskCacheExtensionPreservation:
    """Verify cache preserves the actual audio extension, not just .m4a."""

    @pytest.mark.parametrize("ext", [".opus", ".webm", ".mp3", ".ogg", ".m4a"])
    async def test_put_preserves_extension(self, tmp_path: Path, ext: str) -> None:
        cache = DiskCache(cache_dir=tmp_path / "cache", max_size_bytes=10**9)
        src = tmp_path / f"audio{ext}"
        src.write_bytes(b"fake audio data")
        result = await cache.put("testvid123", src)
        assert result.suffix == ext
        assert result.name == f"testvid123{ext}"

    async def test_get_finds_opus_file(self, tmp_path: Path) -> None:
        cache = DiskCache(cache_dir=tmp_path / "cache", max_size_bytes=10**9)
        src = tmp_path / "audio.opus"
        src.write_bytes(b"opus data")
        await cache.put("opusvid123", src)
        result = await cache.get("opusvid123")
        assert result is not None
        assert result.suffix == ".opus"

    async def test_exists_finds_webm_file(self, tmp_path: Path) -> None:
        cache = DiskCache(cache_dir=tmp_path / "cache", max_size_bytes=10**9)
        src = tmp_path / "audio.webm"
        src.write_bytes(b"webm data")
        await cache.put("webmvid123", src)
        assert await cache.exists("webmvid123") is True

    async def test_evict_removes_mp3_file(self, tmp_path: Path) -> None:
        cache = DiskCache(cache_dir=tmp_path / "cache", max_size_bytes=10**9)
        src = tmp_path / "audio.mp3"
        src.write_bytes(b"mp3 data")
        await cache.put("mp3vid1234", src)
        await cache.evict("mp3vid1234")
        assert await cache.exists("mp3vid1234") is False

    async def test_total_size_counts_all_extensions(self, tmp_path: Path) -> None:
        cache = DiskCache(cache_dir=tmp_path / "cache", max_size_bytes=10**9)
        for ext, vid in [
            (".m4a", "vid1vid1vid"),
            (".opus", "vid2vid2vid"),
            (".mp3", "vid3vid3vid"),
        ]:
            src = tmp_path / f"a{ext}"
            src.write_bytes(b"X" * 100)
            await cache.put(vid, src)
        total = await cache.total_size_bytes()
        assert total == 300

    async def test_lru_eviction_works_across_extensions(self, tmp_path: Path) -> None:
        """LRU eviction should consider files of all extensions."""
        cache = DiskCache(cache_dir=tmp_path / "cache", max_size_bytes=250)
        # Put two files of different formats
        for ext, vid in [(".opus", "AAAAAAAAAAA"), (".mp3", "BBBBBBBBBBB")]:
            src = tmp_path / f"a{ext}"
            src.write_bytes(b"X" * 100)
            await cache.put(vid, src)

        import asyncio

        await asyncio.sleep(0.01)
        await cache.get("AAAAAAAAAAA")  # touch first to make it MRU
        await asyncio.sleep(0.01)

        # Third file pushes over limit
        src = tmp_path / "a.m4a"
        src.write_bytes(b"X" * 100)
        await cache.put("CCCCCCCCCCC", src)

        assert not await cache.exists("BBBBBBBBBBB")
        assert await cache.exists("AAAAAAAAAAA")
        assert await cache.exists("CCCCCCCCCCC")


# ---------------------------------------------------------------------------
# cleanup_stale_tmp
# ---------------------------------------------------------------------------


class TestCleanupStaleTmp:
    async def test_removes_old_files(self, tmp_path: Path) -> None:
        from src.cache.disk import cleanup_stale_tmp

        old_file = tmp_path / "old.m4a"
        old_file.write_bytes(b"old")
        # Set mtime to 2 hours ago
        import os
        import time

        old_time = time.time() - 7200
        os.utime(old_file, (old_time, old_time))

        new_file = tmp_path / "new.m4a"
        new_file.write_bytes(b"new")

        count = await cleanup_stale_tmp(tmp_path, max_age_seconds=3600)
        assert count == 1
        assert not old_file.exists()
        assert new_file.exists()

    async def test_returns_zero_for_nonexistent_dir(self) -> None:
        from pathlib import Path

        from src.cache.disk import cleanup_stale_tmp

        count = await cleanup_stale_tmp(Path("/nonexistent/dir"), max_age_seconds=3600)
        assert count == 0


class TestPutRemovesStaleExtension:
    """put() should remove old variant when extension changes."""

    async def test_old_m4a_removed_when_mp3_cached(
        self, disk_cache: DiskCache, cache_dir: Path, tmp_path: Path
    ) -> None:
        vid = "sameid12345a"
        # Cache as .m4a first
        m4a = tmp_path / "first.m4a"
        m4a.write_bytes(b"m4a_data")
        await disk_cache.put(vid, m4a)
        assert (cache_dir / f"{vid}.m4a").exists()

        # Now cache as .mp3
        mp3 = tmp_path / "second.mp3"
        mp3.write_bytes(b"mp3_data")
        await disk_cache.put(vid, mp3)

        # Old .m4a should be gone, new .mp3 should exist
        assert not (cache_dir / f"{vid}.m4a").exists()
        assert (cache_dir / f"{vid}.mp3").exists()

        # get() should return the .mp3
        result = await disk_cache.get(vid)
        assert result is not None
        assert result.suffix == ".mp3"

    async def test_same_extension_not_removed(
        self, disk_cache: DiskCache, cache_dir: Path, tmp_path: Path
    ) -> None:
        vid = "sameid12345b"
        m4a_1 = tmp_path / "v1.m4a"
        m4a_1.write_bytes(b"v1_data")
        await disk_cache.put(vid, m4a_1)

        m4a_2 = tmp_path / "v2.m4a"
        m4a_2.write_bytes(b"v2_data")
        await disk_cache.put(vid, m4a_2)

        # File should exist (overwritten, not deleted)
        assert (cache_dir / f"{vid}.m4a").exists()
