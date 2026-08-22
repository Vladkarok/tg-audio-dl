"""Abstract cache backend interface."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path

# YouTube 11-char IDs, SoundCloud numeric IDs, and sc_slug cache keys (up to 64 chars).
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_video_id(video_id: str) -> None:
    """Raise ValueError if video_id does not match the expected pattern."""
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise ValueError(
            f"Invalid video_id {video_id!r}: must match ^[A-Za-z0-9_-]{{1,64}}$"
        )


class CacheBackend(ABC):
    @abstractmethod
    async def get(self, video_id: str) -> Path | None:
        """Return path to cached file or None if not cached."""

    @abstractmethod
    async def put(self, video_id: str, file_path: Path) -> Path:
        """Store file in cache, return final cached path."""

    @abstractmethod
    async def exists(self, video_id: str) -> bool:
        """Check if video_id is in cache."""

    @abstractmethod
    async def evict(self, video_id: str) -> None:
        """Remove a specific entry from cache."""

    @abstractmethod
    async def total_size_bytes(self) -> int:
        """Return total size of all cached files in bytes."""

    @abstractmethod
    async def get_file_id(self, video_id: str) -> str | None:
        """Return stored Telegram file_id or None if not stored."""

    @abstractmethod
    async def store_file_id(self, video_id: str, file_id: str) -> None:
        """Persist a Telegram file_id for the given video_id."""

    async def get_chapters(self, video_id: str) -> tuple[tuple[int, str], ...] | None:
        """Return cached chapters or None. Default: not stored."""
        return None

    async def store_chapters(  # noqa: B027
        self, video_id: str, chapters: tuple[tuple[int, str], ...]
    ) -> None:
        """Persist chapters for a video_id. Default: no-op."""

    async def find_by_file_id(self, file_id: str) -> str | None:
        """Return the video_id a stored Telegram file_id belongs to.

        Lets a reply to an audio message the bot already sent be traced back to
        its cache key. Default: no reverse lookup available.
        """
        return None

    async def get_manual_chapters(
        self, video_id: str
    ) -> tuple[tuple[int, str], ...] | None:
        """Return hand-entered chapters, or None when none are stored.

        These override upstream chapters at display time. Default: not stored.
        """
        return None

    async def store_manual_chapters(  # noqa: B027
        self, video_id: str, chapters: tuple[tuple[int, str], ...]
    ) -> None:
        """Persist hand-entered chapters for a video_id. Default: no-op."""

    async def delete_manual_chapters(self, video_id: str) -> bool:
        """Remove hand-entered chapters. Default: nothing stored, so False."""
        return False
