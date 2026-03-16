"""Abstract cache backend interface."""

from abc import ABC, abstractmethod
from pathlib import Path


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
