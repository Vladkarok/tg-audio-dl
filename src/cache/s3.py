"""Optional S3 cache backend.

Uses the synchronous ``boto3`` client wrapped in ``asyncio.to_thread`` so that
S3 I/O never blocks the event loop.  This pattern is also fully compatible with
``moto`` in tests.

All S3 errors are caught and logged; operations degrade gracefully
(get → None, put → original path, exists → False, total_size → 0).
"""

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from src.cache.base import CacheBackend, validate_video_id

logger = logging.getLogger(__name__)

_S3_KEY_PREFIX = "audio/"

# Audio extensions to probe when the extension is unknown, in priority order
_AUDIO_EXTENSIONS = (".m4a", ".opus", ".webm", ".mp3", ".ogg")


def _s3_key(video_id: str, suffix: str = ".m4a") -> str:
    return f"{_S3_KEY_PREFIX}{video_id}{suffix}"


async def _run_blocking[**P, T](
    func: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
) -> T:
    """Run blocking work off the event loop.

    Kept as a small wrapper so tests can replace the thread offload strategy
    without patching ``asyncio`` globally.
    """
    return await asyncio.to_thread(func, *args, **kwargs)


class S3Cache(CacheBackend):
    """S3-backed cache using boto3 wrapped with ``asyncio.to_thread``."""

    def __init__(self, bucket: str, region: str, local_tmp_dir: Path) -> None:
        self._bucket = bucket
        self._region = region
        self._local_tmp_dir = local_tmp_dir
        self._client = boto3.client("s3", region_name=region)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_s3_key(self, video_id: str) -> str | None:
        """Probe S3 for the first existing key across known audio extensions.

        Checks .m4a first (most common), then other formats. Returns the full
        S3 key or None if no object exists.
        """
        for ext in _AUDIO_EXTENSIONS:
            key = _s3_key(video_id, ext)
            try:
                self._client.head_object(Bucket=self._bucket, Key=key)
                return key
            except ClientError as exc:
                error_code = exc.response.get("Error", {}).get("Code", "")
                if error_code in ("404", "NoSuchKey"):
                    continue
                # Unexpected error — stop probing
                raise
        return None

    @staticmethod
    def _extract_error(exc: ClientError) -> tuple[str, str]:
        """Extract error code and message from a ClientError."""
        error = exc.response.get("Error", {})
        return error.get("Code", ""), error.get("Message", "")

    # ------------------------------------------------------------------
    # Startup validation
    # ------------------------------------------------------------------

    async def probe(self) -> None:
        """Verify S3 credentials and object-level access. Raises on failure.

        Uses head_object on a known-absent sentinel key rather than
        head_bucket (which requires s3:ListBucket).  A 404 / NoSuchKey
        response confirms the client can reach the bucket and has object-
        level permissions; anything else (403 AccessDenied, network error,
        etc.) is re-raised so startup fails fast instead of silently
        degrading at runtime.  Works with least-privilege IAM policies that
        only grant GetObject/PutObject/DeleteObject/HeadObject.
        """
        _PROBE_KEY = f"{_S3_KEY_PREFIX}.probe-sentinel"

        def _check() -> None:
            try:
                self._client.head_object(Bucket=self._bucket, Key=_PROBE_KEY)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in ("404", "NoSuchKey"):
                    return  # bucket reachable; key simply doesn't exist
                raise  # 403 AccessDenied or unexpected error

        await _run_blocking(_check)

    # ------------------------------------------------------------------
    # CacheBackend interface
    # ------------------------------------------------------------------

    async def get(self, video_id: str) -> Path | None:
        """Download object from S3 to local_tmp_dir; return path or None."""
        validate_video_id(video_id)

        def _download() -> Path | None:
            key = self._find_s3_key(video_id)
            if key is None:
                return None
            # Derive local extension from the discovered S3 key.
            # Use a unique temp name to prevent concurrent get() calls for
            # the same video_id from overwriting each other's partial download.
            suffix = Path(key).suffix
            tmp_name = f"{video_id}_{uuid.uuid4().hex[:8]}{suffix}"
            local_path = self._local_tmp_dir / tmp_name
            self._client.download_file(self._bucket, key, str(local_path))
            return local_path

        try:
            return await _run_blocking(_download)
        except ClientError as exc:
            code, msg = self._extract_error(exc)
            if code in ("404", "NoSuchKey"):
                return None
            logger.warning(
                "S3Cache.get failed for %s: code=%s msg=%s", video_id, code, msg
            )
            return None
        except Exception:
            logger.warning("S3Cache.get failed for %s", video_id, exc_info=True)
            return None

    async def put(self, video_id: str, file_path: Path) -> Path:
        """Upload *file_path* to S3 preserving extension; return original path."""
        validate_video_id(video_id)
        key = _s3_key(video_id, file_path.suffix)

        def _upload() -> None:
            # Probe for stale variant *before* upload so the lookup sees
            # the old extension, not the new one (which has higher priority
            # and would shadow the old key in _find_s3_key).
            old_key = self._find_s3_key(video_id)
            # Upload first, then delete — ensures there is always at least
            # one valid object (no availability gap for concurrent get).
            self._client.upload_file(str(file_path), self._bucket, key)
            if old_key is not None and old_key != key:
                with contextlib.suppress(ClientError):
                    self._client.delete_object(Bucket=self._bucket, Key=old_key)

        try:
            await _run_blocking(_upload)
            return file_path
        except ClientError as exc:
            code, msg = self._extract_error(exc)
            logger.warning(
                "S3Cache.put failed for %s: code=%s msg=%s", video_id, code, msg
            )
            return file_path
        except Exception:
            logger.warning("S3Cache.put failed for %s", video_id, exc_info=True)
            return file_path

    async def exists(self, video_id: str) -> bool:
        validate_video_id(video_id)

        def _probe() -> bool:
            return self._find_s3_key(video_id) is not None

        try:
            return await _run_blocking(_probe)
        except ClientError as exc:
            code, msg = self._extract_error(exc)
            logger.warning(
                "S3Cache.exists failed for %s: code=%s msg=%s", video_id, code, msg
            )
            return False
        except Exception:
            logger.warning("S3Cache.exists failed for %s", video_id, exc_info=True)
            return False

    async def evict(self, video_id: str) -> None:
        validate_video_id(video_id)

        def _delete() -> None:
            key = self._find_s3_key(video_id)
            if key is not None:
                self._client.delete_object(Bucket=self._bucket, Key=key)

        try:
            await _run_blocking(_delete)
        except ClientError as exc:
            code, msg = self._extract_error(exc)
            logger.warning(
                "S3Cache.evict failed for %s: code=%s msg=%s", video_id, code, msg
            )
        except Exception:
            logger.warning("S3Cache.evict failed for %s", video_id, exc_info=True)

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
            return await _run_blocking(_sum_sizes)
        except ClientError as exc:
            code, msg = self._extract_error(exc)
            logger.warning("S3Cache.total_size_bytes failed: code=%s msg=%s", code, msg)
            return 0
        except Exception:
            logger.warning("S3Cache.total_size_bytes failed", exc_info=True)
            return 0
