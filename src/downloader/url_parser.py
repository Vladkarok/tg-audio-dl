"""YouTube and SoundCloud URL parser and normalizer.

YouTube classification rules (in priority order):
1. RADIO_MIX  — list=RD*, list=RDMM*, or start_radio=1 with a v= param
2. PLAYLIST   — /playlist?list=PL* (no v=)
3. SINGLE     — v= with list=PL*/FL*/UU* (user shared specific video)
4. SINGLE     — bare v=, youtu.be/<id>, shorts/<id>

SoundCloud classification rules:
1. PLAYLIST   — /artist/sets/setname
2. SINGLE     — /artist/track  (non-reserved path)
3. SINGLE     — on.soundcloud.com/<shortcode>

Security: raw URLs are never passed to a shell.
          malformed / non-YouTube / non-SoundCloud URLs return None, never raise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TypeGuard
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Public data model
# ---------------------------------------------------------------------------


class URLType(Enum):
    SINGLE = auto()
    PLAYLIST = auto()
    RADIO_MIX = auto()


class Platform(Enum):
    YOUTUBE = "youtube"
    SOUNDCLOUD = "soundcloud"


@dataclass(frozen=True)
class ParsedURL:
    url_type: URLType
    video_id: str | None
    playlist_id: str | None
    canonical_url: str
    original_url: str
    platform: Platform = field(default=Platform.YOUTUBE)


# ---------------------------------------------------------------------------
# Internal constants — YouTube
# ---------------------------------------------------------------------------

# Accepted YouTube hostnames (exact match required — no suffix tricks)
_YOUTUBE_HOSTS: frozenset[str] = frozenset(
    {
        "www.youtube.com",
        "youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }
)

# Query parameters that carry no semantic meaning and must be stripped
_TRACKING_PARAMS: frozenset[str] = frozenset(
    {"si", "feature", "index", "t", "rv", "start_radio"}
)

# Regex to find candidate YouTube URLs inside free-form text
_YT_URL_RE = re.compile(
    r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)"
    r"(?:/[^\s\"'<>]*)?"
    r"(?:\?[^\s\"'<>]*)?"
)

# ---------------------------------------------------------------------------
# Internal constants — SoundCloud
# ---------------------------------------------------------------------------

_SOUNDCLOUD_HOSTS: frozenset[str] = frozenset(
    {
        "soundcloud.com",
        "www.soundcloud.com",
    }
)

# Path segments that are user-profile pages, not downloadable tracks
_SC_RESERVED_PATHS: frozenset[str] = frozenset(
    {
        "likes",
        "reposts",
        "followers",
        "following",
        "tracks",
        "albums",
        "sets",
        "popular-tracks",
        "comments",
    }
)

# Regex to find candidate SoundCloud URLs inside free-form text
_SC_URL_RE = re.compile(
    r"https?://(?:www\.)?soundcloud\.com(?:/[^\s\"'<>]*)?"
    r"|https?://on\.soundcloud\.com(?:/[^\s\"'<>]*)?"
)

# Regex for safe cache key characters
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


# ---------------------------------------------------------------------------
# Private helpers — YouTube
# ---------------------------------------------------------------------------


def _is_youtube_host(host: str) -> bool:
    """Return True only for legitimate YouTube hostnames."""
    return host in _YOUTUBE_HOSTS


def _first_param(params: dict[str, list[str]], key: str) -> str | None:
    """Return first value for *key* from parse_qs result, or None."""
    values = params.get(key)
    return values[0] if values else None


def _extract_video_id_from_path(path: str) -> str | None:
    """Extract video ID from /shorts/<id> or /youtu.be/<id> path segments."""
    shorts_match = re.fullmatch(r"/shorts/([A-Za-z0-9_-]{11})", path)
    if shorts_match:
        return shorts_match.group(1)
    bare_match = re.fullmatch(r"/([A-Za-z0-9_-]{11})", path)
    if bare_match:
        return bare_match.group(1)
    return None


def _make_canonical_single(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _make_canonical_playlist(playlist_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={playlist_id}"


def _is_valid_video_id(vid: str | None) -> TypeGuard[str]:
    """YouTube video IDs are exactly 11 URL-safe base64 characters."""
    if not vid:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", vid))


# ---------------------------------------------------------------------------
# Private helpers — SoundCloud
# ---------------------------------------------------------------------------


def _make_sc_video_id(artist: str, track: str) -> str:
    """Derive a stable cache key from SoundCloud artist/track slugs.

    Result is always safe for our filesystem cache: [A-Za-z0-9_-], max 64 chars.
    """
    raw = f"sc_{artist}_{track}"
    safe = _SAFE_ID_RE.sub("_", raw)
    return safe[:64]


# ---------------------------------------------------------------------------
# Public API — YouTube
# ---------------------------------------------------------------------------


def parse_youtube_url(raw_url: str) -> ParsedURL | None:
    """Parse and classify a YouTube URL.

    Returns None if the input is not a recognised YouTube URL.
    Never raises; malformed input is silently rejected.
    """
    if not raw_url:
        return None

    try:
        parsed = urlparse(raw_url)
    except Exception:  # noqa: BLE001
        return None

    if not _is_youtube_host(parsed.netloc):
        return None

    try:
        params = parse_qs(parsed.query, keep_blank_values=False)
    except Exception:  # noqa: BLE001
        return None

    video_id: str | None = _first_param(params, "v")
    list_id: str | None = _first_param(params, "list")

    if not video_id:
        video_id = _extract_video_id_from_path(parsed.path)

    # 1. RADIO_MIX
    is_radio = (list_id is not None and list_id.startswith("RD")) or (
        "start_radio" in params
    )
    if is_radio:
        if not _is_valid_video_id(video_id):
            return None
        return ParsedURL(
            url_type=URLType.RADIO_MIX,
            video_id=video_id,
            playlist_id=None,
            canonical_url=_make_canonical_single(video_id),
            original_url=raw_url,
            platform=Platform.YOUTUBE,
        )

    # 2. PLAYLIST
    if parsed.path == "/playlist" and list_id and not video_id:
        return ParsedURL(
            url_type=URLType.PLAYLIST,
            video_id=None,
            playlist_id=list_id,
            canonical_url=_make_canonical_playlist(list_id),
            original_url=raw_url,
            platform=Platform.YOUTUBE,
        )

    # 3. SINGLE
    if _is_valid_video_id(video_id):
        return ParsedURL(
            url_type=URLType.SINGLE,
            video_id=video_id,
            playlist_id=None,
            canonical_url=_make_canonical_single(video_id),
            original_url=raw_url,
            platform=Platform.YOUTUBE,
        )

    return None


# ---------------------------------------------------------------------------
# Public API — SoundCloud
# ---------------------------------------------------------------------------


def parse_soundcloud_url(raw_url: str) -> ParsedURL | None:
    """Parse and classify a SoundCloud URL.

    Returns None if the input is not a recognised SoundCloud URL.
    Never raises; malformed input is silently rejected.
    """
    if not raw_url:
        return None

    try:
        parsed = urlparse(raw_url)
    except Exception:  # noqa: BLE001
        return None

    host = parsed.netloc

    # on.soundcloud.com short URLs — can't derive slug, no video_id
    if host == "on.soundcloud.com":
        return ParsedURL(
            url_type=URLType.SINGLE,
            video_id=None,
            playlist_id=None,
            canonical_url=raw_url,
            original_url=raw_url,
            platform=Platform.SOUNDCLOUD,
        )

    if host not in _SOUNDCLOUD_HOSTS:
        return None

    # Parse path segments
    path = parsed.path.rstrip("/")
    parts = [p for p in path.split("/") if p]

    if len(parts) < 2:
        # Just /artist or root — not downloadable
        return None

    artist = parts[0]

    # /artist/sets/setname → PLAYLIST
    if parts[1] == "sets" and len(parts) >= 3:
        set_name = parts[2]
        return ParsedURL(
            url_type=URLType.PLAYLIST,
            video_id=None,
            playlist_id=f"{artist}/sets/{set_name}",
            canonical_url=raw_url,
            original_url=raw_url,
            platform=Platform.SOUNDCLOUD,
        )

    # /artist/track → SINGLE (skip reserved profile-page paths)
    track = parts[1]
    if track in _SC_RESERVED_PATHS:
        return None

    video_id = _make_sc_video_id(artist, track)
    return ParsedURL(
        url_type=URLType.SINGLE,
        video_id=video_id,
        playlist_id=None,
        canonical_url=raw_url,
        original_url=raw_url,
        platform=Platform.SOUNDCLOUD,
    )


# ---------------------------------------------------------------------------
# Public API — combined extractor
# ---------------------------------------------------------------------------


def extract_media_urls(text: str) -> list[ParsedURL]:
    """Extract and parse all YouTube and SoundCloud URLs found in *text*.

    Returns a list of ParsedURL objects in order of appearance.
    Unrecognised URLs are silently skipped.
    """
    if not text:
        return []

    results: list[ParsedURL] = []

    for match in _YT_URL_RE.finditer(text):
        parsed = parse_youtube_url(match.group(0))
        if parsed is not None:
            results.append(parsed)

    for match in _SC_URL_RE.finditer(text):
        parsed = parse_soundcloud_url(match.group(0))
        if parsed is not None:
            results.append(parsed)

    return results
