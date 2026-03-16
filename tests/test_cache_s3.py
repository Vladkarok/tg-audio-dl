"""Tests for src/cache/s3.py — Phase 3.

Uses moto to mock AWS S3 so no real credentials are needed.
"""

from pathlib import Path
from unittest.mock import MagicMock

import boto3
import botocore.exceptions
import pytest
from moto import mock_aws

from src.cache.s3 import S3Cache

BUCKET = "test-audio-bucket"
REGION = "us-east-1"


@pytest.fixture
def source_file(tmp_path: Path) -> Path:
    f = tmp_path / "source.m4a"
    f.write_bytes(b"fake_audio_bytes_1234")
    return f


@pytest.fixture
def local_tmp(tmp_path: Path) -> Path:
    d = tmp_path / "s3_tmp"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide fake AWS credentials so moto does not complain."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


def _make_bucket() -> boto3.client:
    """Create a moto-mocked bucket and return the client."""
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET)
    return s3


# ---------------------------------------------------------------------------
# put / get / exists / evict / total_size_bytes
# Helper: each test starts its own mock_aws context to avoid
# issues with pytest-asyncio + moto decorator interaction.
# ---------------------------------------------------------------------------


class TestS3CachePutGetExistsEvict:
    async def test_s3_put_uploads_to_bucket(
        self, local_tmp: Path, source_file: Path
    ) -> None:
        with mock_aws():
            s3_client = _make_bucket()
            cache = S3Cache(bucket=BUCKET, region=REGION, local_tmp_dir=local_tmp)

            await cache.put("dQw4w9WgXcQ", source_file)

            response = s3_client.get_object(Bucket=BUCKET, Key="audio/dQw4w9WgXcQ.m4a")
            assert response["Body"].read() == source_file.read_bytes()

    async def test_s3_get_downloads_from_bucket(
        self, local_tmp: Path, source_file: Path
    ) -> None:
        with mock_aws():
            _make_bucket()
            cache = S3Cache(bucket=BUCKET, region=REGION, local_tmp_dir=local_tmp)

            await cache.put("dQw4w9WgXcQ", source_file)
            result = await cache.get("dQw4w9WgXcQ")

            assert result is not None
            assert result.exists()
            assert result.read_bytes() == source_file.read_bytes()

    async def test_s3_get_returns_none_for_missing(self, local_tmp: Path) -> None:
        with mock_aws():
            _make_bucket()
            cache = S3Cache(bucket=BUCKET, region=REGION, local_tmp_dir=local_tmp)

            result = await cache.get("missingVidX")
            assert result is None

    async def test_s3_exists_true(self, local_tmp: Path, source_file: Path) -> None:
        with mock_aws():
            _make_bucket()
            cache = S3Cache(bucket=BUCKET, region=REGION, local_tmp_dir=local_tmp)

            await cache.put("dQw4w9WgXcQ", source_file)
            assert await cache.exists("dQw4w9WgXcQ") is True

    async def test_s3_exists_false(self, local_tmp: Path) -> None:
        with mock_aws():
            _make_bucket()
            cache = S3Cache(bucket=BUCKET, region=REGION, local_tmp_dir=local_tmp)

            assert await cache.exists("missingVidX") is False

    async def test_s3_evict_deletes_object(
        self, local_tmp: Path, source_file: Path
    ) -> None:
        with mock_aws():
            _make_bucket()
            cache = S3Cache(bucket=BUCKET, region=REGION, local_tmp_dir=local_tmp)

            await cache.put("dQw4w9WgXcQ", source_file)
            await cache.evict("dQw4w9WgXcQ")
            assert await cache.exists("dQw4w9WgXcQ") is False

    async def test_s3_total_size(self, local_tmp: Path, tmp_path: Path) -> None:
        with mock_aws():
            _make_bucket()
            cache = S3Cache(bucket=BUCKET, region=REGION, local_tmp_dir=local_tmp)

            f1 = tmp_path / "a.m4a"
            f2 = tmp_path / "b.m4a"
            f1.write_bytes(b"A" * 100)
            f2.write_bytes(b"B" * 200)
            await cache.put("AAAAAAAAAAA", f1)
            await cache.put("BBBBBBBBBBB", f2)

            total = await cache.total_size_bytes()
            assert total == 300


# ---------------------------------------------------------------------------
# Graceful error handling
# ---------------------------------------------------------------------------


def _make_client_error(code: str) -> botocore.exceptions.ClientError:
    return botocore.exceptions.ClientError(
        {"Error": {"Code": code, "Message": "test"}}, "TestOp"
    )


def _make_cache_with_mock_client(local_tmp: Path) -> tuple["S3Cache", MagicMock]:
    """Create an S3Cache with a mock _client, using mock_aws for construction."""
    with mock_aws():
        _make_bucket()
        cache = S3Cache(bucket="bucket", region=REGION, local_tmp_dir=local_tmp)
    mock_client = MagicMock()
    cache._client = mock_client
    return cache, mock_client


class TestS3CacheValidatesVideoId:
    """S3Cache rejects invalid video_id values before touching S3."""

    async def test_s3_rejects_invalid_video_id_get(self, local_tmp: Path) -> None:
        with mock_aws():
            _make_bucket()
            cache = S3Cache(bucket=BUCKET, region=REGION, local_tmp_dir=local_tmp)
            with pytest.raises(ValueError, match="Invalid video_id"):
                await cache.get("../evil")

    async def test_s3_rejects_invalid_video_id_put(
        self, local_tmp: Path, source_file: Path
    ) -> None:
        with mock_aws():
            _make_bucket()
            cache = S3Cache(bucket=BUCKET, region=REGION, local_tmp_dir=local_tmp)
            with pytest.raises(ValueError, match="Invalid video_id"):
                await cache.put("../evil", source_file)

    async def test_s3_rejects_invalid_video_id_exists(self, local_tmp: Path) -> None:
        with mock_aws():
            _make_bucket()
            cache = S3Cache(bucket=BUCKET, region=REGION, local_tmp_dir=local_tmp)
            with pytest.raises(ValueError, match="Invalid video_id"):
                await cache.exists("has/slash")

    async def test_s3_rejects_invalid_video_id_evict(self, local_tmp: Path) -> None:
        with mock_aws():
            _make_bucket()
            cache = S3Cache(bucket=BUCKET, region=REGION, local_tmp_dir=local_tmp)
            with pytest.raises(ValueError, match="Invalid video_id"):
                await cache.evict("a" * 65)


class TestS3CacheGracefulErrors:
    async def test_s3_get_graceful_on_error(self, local_tmp: Path) -> None:
        """Generic Exception in get() → returns None."""
        cache, mock_client = _make_cache_with_mock_client(local_tmp)
        mock_client.download_file.side_effect = Exception("network error")
        result = await cache.get("dQw4w9WgXcQ")
        assert result is None

    async def test_s3_get_client_error_non_404(self, local_tmp: Path) -> None:
        """Non-404 ClientError in get() → warning logged, returns None."""
        cache, mock_client = _make_cache_with_mock_client(local_tmp)
        mock_client.download_file.side_effect = _make_client_error("403")
        result = await cache.get("dQw4w9WgXcQ")
        assert result is None

    async def test_s3_put_graceful_on_error(
        self, local_tmp: Path, source_file: Path
    ) -> None:
        """If S3 upload fails, put() logs a warning but does not raise."""
        cache, mock_client = _make_cache_with_mock_client(local_tmp)
        mock_client.upload_file.side_effect = Exception("upload error")
        result = await cache.put("dQw4w9WgXcQ", source_file)
        assert result == source_file

    async def test_s3_exists_client_error_non_404(self, local_tmp: Path) -> None:
        """Non-404 ClientError in exists() → warning logged, returns False."""
        cache, mock_client = _make_cache_with_mock_client(local_tmp)
        mock_client.head_object.side_effect = _make_client_error("403")
        result = await cache.exists("dQw4w9WgXcQ")
        assert result is False

    async def test_s3_exists_generic_error(self, local_tmp: Path) -> None:
        """Generic Exception in exists() → returns False."""
        cache, mock_client = _make_cache_with_mock_client(local_tmp)
        mock_client.head_object.side_effect = Exception("boom")
        result = await cache.exists("dQw4w9WgXcQ")
        assert result is False

    async def test_s3_evict_graceful_on_error(self, local_tmp: Path) -> None:
        """Exception in evict() → warning logged, no raise."""
        cache, mock_client = _make_cache_with_mock_client(local_tmp)
        mock_client.delete_object.side_effect = Exception("delete failed")
        await cache.evict("dQw4w9WgXcQ")  # must not raise

    async def test_s3_total_size_graceful_on_error(self, local_tmp: Path) -> None:
        """Exception in total_size_bytes() → returns 0."""
        cache, mock_client = _make_cache_with_mock_client(local_tmp)
        mock_client.get_paginator.side_effect = Exception("list failed")
        total = await cache.total_size_bytes()
        assert total == 0

    async def test_s3_get_file_id_always_returns_none(self, local_tmp: Path) -> None:
        """S3Cache.get_file_id always returns None (file_ids are local)."""
        cache, _ = _make_cache_with_mock_client(local_tmp)
        result = await cache.get_file_id("dQw4w9WgXcQ")
        assert result is None

    async def test_s3_store_file_id_is_noop(self, local_tmp: Path) -> None:
        """S3Cache.store_file_id is a no-op — does not raise."""
        cache, _ = _make_cache_with_mock_client(local_tmp)
        await cache.store_file_id("dQw4w9WgXcQ", "some_file_id")
