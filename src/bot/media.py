"""Audio tag and cover-art extraction helpers (mutagen + Pillow)."""

from __future__ import annotations

import io
import logging
from pathlib import Path

import mutagen
from PIL import Image
from telegram import InputFile

logger = logging.getLogger(__name__)

_THUMB_MAX_SIZE = 320  # Telegram max thumbnail dimension (px)
_THUMB_QUALITY = 80  # JPEG quality for resized thumbnails


def _extract_audio_metadata(file_path: Path) -> tuple[str | None, str | None]:
    """Extract title and artist from audio tags. Returns (title, artist).

    Supports M4A/MP4, Opus/Vorbis (webm/ogg), and MP3 (ID3) via mutagen
    auto-detection.
    """
    try:
        audio = mutagen.File(file_path)  # type: ignore[attr-defined]
        if audio is None or audio.tags is None:
            return None, None
        tags = audio.tags
        # M4A / MP4
        if hasattr(tags, "get") and "\xa9nam" in tags:
            title = tags.get("\xa9nam", [None])[0]
            artist = tags.get("\xa9ART", [None])[0]
            return title, artist
        # Vorbis (Opus, OGG, WebM)
        if hasattr(tags, "get") and "title" in tags:
            title = tags.get("title", [None])[0]
            artist = tags.get("artist", [None])[0]
            return title, artist
        # ID3 (MP3)
        if hasattr(tags, "getall"):
            tit2 = tags.getall("TIT2")
            tpe1 = tags.getall("TPE1")
            title = str(tit2[0]) if tit2 else None
            artist = str(tpe1[0]) if tpe1 else None
            return title, artist
        return None, None
    except Exception:
        logger.debug("Could not read metadata from %s", file_path, exc_info=True)
        return None, None


def _resize_thumbnail(raw: bytes) -> bytes:
    """Resize cover art to fit Telegram's 320x320 thumbnail limit.

    Returns JPEG bytes. If the image is already small enough, returns it as-is.
    """
    img = Image.open(io.BytesIO(raw))
    if img.width <= _THUMB_MAX_SIZE and img.height <= _THUMB_MAX_SIZE:
        return raw
    img.thumbnail((_THUMB_MAX_SIZE, _THUMB_MAX_SIZE), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=_THUMB_QUALITY)
    return buf.getvalue()


def _extract_thumbnail(file_path: Path) -> InputFile | None:
    """Extract embedded cover art from an audio file as an InputFile for Telegram.

    Supports M4A (covr tag), Opus/Vorbis (metadata_block_picture), and MP3 (APIC).
    Images are resized to max 320x320 to comply with Telegram's thumbnail limits.
    """
    try:
        audio = mutagen.File(file_path)  # type: ignore[attr-defined]
        if audio is None or audio.tags is None:
            return None
        tags = audio.tags
        raw: bytes | None = None
        # M4A / MP4: covr tag
        if hasattr(tags, "get") and "covr" in tags and tags["covr"]:
            raw = bytes(tags["covr"][0])
        # Vorbis (Opus/OGG): metadata_block_picture
        elif hasattr(tags, "get") and "metadata_block_picture" in tags:
            import base64

            from mutagen.flac import Picture

            pic_data = base64.b64decode(tags["metadata_block_picture"][0])
            picture = Picture(pic_data)  # type: ignore[no-untyped-call]
            raw = picture.data
        # ID3 (MP3): APIC frames
        elif hasattr(tags, "getall"):
            apic_frames = tags.getall("APIC")
            if apic_frames:
                raw = apic_frames[0].data

        if raw is None:
            return None
        resized = _resize_thumbnail(raw)
        return InputFile(io.BytesIO(resized), filename="cover.jpg")
    except Exception:
        logger.debug("Could not extract thumbnail from %s", file_path, exc_info=True)
    return None
