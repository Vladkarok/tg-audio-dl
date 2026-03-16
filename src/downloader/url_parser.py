"""YouTube URL parser and normalizer.

Classification rules (in priority order):
1. RADIO_MIX  — list=RD*, list=RDMM*, or start_radio=1 with a v= param
2. PLAYLIST   — /playlist?list=PL* (no v=)
3. SINGLE     — v= with list=PL*/FL*/UU* (user shared specific video)
4. SINGLE     — bare v=, youtu.be/<id>, shorts/<id>

Security: raw URLs are never passed to a shell.
          malformed / non-YouTube URLs return None, never raise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, auto
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Public data model
# ---------------------------------------------------------------------------


class URLType(Enum):
    SINGLE = auto()
    PLAYLIST = auto()
    RADIO_MIX = auto()


@dataclass(frozen=True)
class ParsedURL:
    url_type: URLType
    video_id: str | None
    playlist_id: str | None
    canonical_url: str
    original_url: str


# ---------------------------------------------------------------------------
# Internal constants
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

# Regex to find candidate URLs inside free-form text
_URL_RE = re.compile(
    r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)"
    r"(?:/[^\s\"'<>]*)?"
    r"(?:\?[^\s\"'<>]*)?"
)


# ---------------------------------------------------------------------------
# Private helpers
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
    # /shorts/<id>
    shorts_match = re.fullmatch(r"/shorts/([A-Za-z0-9_-]{11})", path)
    if shorts_match:
        return shorts_match.group(1)
    # bare path used by youtu.be: /<id>
    bare_match = re.fullmatch(r"/([A-Za-z0-9_-]{11})", path)
    if bare_match:
        return bare_match.group(1)
    return None


def _make_canonical_single(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _make_canonical_playlist(playlist_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={playlist_id}"


def _is_valid_video_id(vid: str | None) -> bool:
    """YouTube video IDs are exactly 11 URL-safe base64 characters."""
    if not vid:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{11}", vid))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_youtube_url(raw_url: str) -> ParsedURL | None:
    """Parse and classify a YouTube URL.

    Returns None if the input is not a recognised YouTube URL.
    Never raises; malformed input is silently rejected.

    Classification priority:
    - RADIO_MIX: list=RD*/RDMM* OR start_radio=1  (v= required)
    - PLAYLIST:  /playlist?list=PL* with no v=
    - SINGLE:    v= present (with any list=PL*/FL*/UU* ignored)
                 or youtu.be/<id>
                 or /shorts/<id>
    """
    if not raw_url:
        return None

    try:
        parsed = urlparse(raw_url)
    except Exception:  # noqa: BLE001
        return None

    # --- Security: validate host -----------------------------------------
    if not _is_youtube_host(parsed.netloc):
        return None

    try:
        params = parse_qs(parsed.query, keep_blank_values=False)
    except Exception:  # noqa: BLE001
        return None

    # --- Extract v= and list= -------------------------------------------
    video_id: str | None = _first_param(params, "v")
    list_id: str | None = _first_param(params, "list")

    # Also try extracting video_id from path (youtu.be / shorts)
    if not video_id:
        video_id = _extract_video_id_from_path(parsed.path)

    # --- Classify --------------------------------------------------------

    # 1. RADIO_MIX: list starts with RD (or RDMM which also starts with RD)
    #    or start_radio=1 is present.  video_id is required.
    is_radio = (list_id is not None and list_id.startswith("RD")) or (
        "start_radio" in params
    )

    if is_radio:
        if not _is_valid_video_id(video_id):
            # Cannot build a meaningful canonical URL without a video id
            return None
        return ParsedURL(
            url_type=URLType.RADIO_MIX,
            video_id=video_id,
            playlist_id=None,
            canonical_url=_make_canonical_single(video_id),
            original_url=raw_url,
        )

    # 2. PLAYLIST: /playlist path with a list=PL* (no v= present)
    if parsed.path == "/playlist" and list_id and not video_id:
        return ParsedURL(
            url_type=URLType.PLAYLIST,
            video_id=None,
            playlist_id=list_id,
            canonical_url=_make_canonical_playlist(list_id),
            original_url=raw_url,
        )

    # 3. SINGLE: v= present (list=PL*/FL*/UU* present means user shared a
    #    specific video from within a playlist — still treat as SINGLE)
    if _is_valid_video_id(video_id):
        return ParsedURL(
            url_type=URLType.SINGLE,
            video_id=video_id,
            playlist_id=None,
            canonical_url=_make_canonical_single(video_id),
            original_url=raw_url,
        )

    return None


def extract_youtube_urls(text: str) -> list[ParsedURL]:
    """Extract and parse all YouTube URLs found in *text*.

    Returns a list of ParsedURL objects (one per URL found, in order).
    Non-YouTube URLs and unrecognised URLs are silently skipped.
    Duplicates are preserved; deduplication is the caller's responsibility.
    """
    if not text:
        return []

    results: list[ParsedURL] = []
    for match in _URL_RE.finditer(text):
        candidate = match.group(0)
        parsed = parse_youtube_url(candidate)
        if parsed is not None:
            results.append(parsed)
    return results
