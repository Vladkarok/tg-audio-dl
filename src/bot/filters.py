"""Custom telegram-ext message filters."""

from __future__ import annotations

from telegram import Message
from telegram.ext.filters import MessageFilter

from src.downloader.url_parser import extract_media_urls


class MediaURLFilter(MessageFilter):
    """Matches messages containing at least one valid YouTube or SoundCloud URL."""

    def filter(self, message: Message) -> bool:
        text = getattr(message, "text", None)
        if not text:
            return False
        return bool(extract_media_urls(text))
