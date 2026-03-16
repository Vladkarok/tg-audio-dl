"""Optional S3 cache backend.

Uses the synchronous ``boto3`` client wrapped in ``asyncio.to_thread`` so that
S3 I/O never blocks the event loop.  This pattern is also fully compatible with
``moto`` in tests.

All S3 errors are caught and logged; operations degrade gracefully
(get → None, put → original path, exists → False, total_size → 0).
"""

import asyncio
import logging
import re
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from src.cache.base import CacheBackend

logger = logging.getLogger(__name__)

_S3_KEY_PREFIX = "audio/"
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{11}$")


def _s3_key(video_id: str) -> str:
    return f"{_S3_KEY_PREFIX}{video_id}.m4a"


class S3Cache(CacheBackend):
    """S3-backed cache using boto3 wrapped with ``asyncio.to_thread``."""

    def __init__(self, bucket: str, region: str, local_tmp_dir: Path) -> None:
        self._bucket = bucket
        self._region = region
        self._local_tmp_dir = local_tmp_dir
        self._client = boto3.client("s3", region_name=region)

    # ------------------------------------------------------------------
    # Input validation
    # ------------------------------------------------------------------

    def _validate_video_id(self, video_id: str) -> None:
        if not _VIDEO_ID_RE.fullmatch(video_id):
            raise ValueError(f"Invalid video_id: {video_id!r}")

    # ------------------------------------------------------------------
    # CacheBackend interface
    # ------------------------------------------------------------------

    async def get(self, video_id: str) -> Path | None:
        """Download object from S3 to local_tmp_dir; return path or None."""
        self._validate_video_id(video_id)
        local_path = self._local_tmp_dir / f"{video_id}.m4a"

        def _download() -> None:
            self._client.download_file(self._bucket, _s3_key(video_id), str(local_path))

        try:
            await asyncio.to_thread(_download)
            return local_path
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey"):
                return None
            logger.warning("S3Cache.get failed for %s: %s", video_id, exc)
            return None
        except Exception as exc:
            logger.warning("S3Cache.get failed for %s: %s", video_id, exc)
            return None

    async def put(self, video_id: str, file_path: Path) -> Path:
        """Upload *file_path* to S3; return original path on failure."""
        self._validate_video_id(video_id)

        def _upload() -> None:
            self._client.upload_file(str(file_path), self._bucket, _s3_key(video_id))

        try:
            await asyncio.to_thread(_upload)
            return file_path
        except Exception as exc:
            logger.warning("S3Cache.put failed for %s: %s", video_id, exc)
            return file_path

    async def exists(self, video_id: str) -> bool:
        self._validate_video_id(video_id)

        def _head() -> bool:
            self._client.head_object(Bucket=self._bucket, Key=_s3_key(video_id))
            return True

        try:
            return await asyncio.to_thread(_head)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey"):
                return False
            logger.warning("S3Cache.exists failed for %s: %s", video_id, exc)
            return False
        except Exception as exc:
            logger.warning("S3Cache.exists failed for %s: %s", video_id, exc)
            return False

    async def evict(self, video_id: str) -> None:
        self._validate_video_id(video_id)

        def _delete() -> None:
            self._client.delete_object(Bucket=self._bucket, Key=_s3_key(video_id))

        try:
            await asyncio.to_thread(_delete)
        except Exception as exc:
            logger.warning("S3Cache.evict failed for %s: %s", video_id, exc)

    async def get_file_id(self, video_id: str) -> str | None:
        """file_ids are local to a single bot instance — not stored in S3."""
        return None

    async def store_file_id(self, video_id: str, file_id: str) -> None:
        """file_ids are local to a single bot instance — not stored in S3."""

    async def total_size_bytes(self) -> int:
        def _sum_sizes() -> int:
            total = 0
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=_S3_KEY_PREFIX):
                for obj in page.get("Contents", []):
                    total += obj.get("Size", 0)
            return total

        try:
            return await asyncio.to_thread(_sum_sizes)
        except Exception as exc:
            logger.warning("S3Cache.total_size_bytes failed: %s", exc)
            return 0
