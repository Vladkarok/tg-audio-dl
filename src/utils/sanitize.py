"""Filename and title sanitization utilities."""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# sanitize_filename
# ---------------------------------------------------------------------------

_SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9 _\-.()\[\]]")
_MULTI_UNDERSCORE_RE = re.compile(r"_+")
_MULTI_SPACE_RE = re.compile(r" +")


def sanitize_filename(name: str, max_length: int = 64) -> str:
    """Remove/replace characters unsafe for filenames and Telegram display.

    - Strip leading/trailing whitespace
    - Replace characters not in [A-Za-z0-9 _\\-.()] with _
    - Collapse multiple underscores/spaces
    - Truncate to max_length
    - Never return empty string (fallback to "audio")
    """
    name = name.strip()
    name = _SAFE_CHARS_RE.sub("_", name)
    name = _MULTI_UNDERSCORE_RE.sub("_", name)
    name = _MULTI_SPACE_RE.sub(" ", name)
    name = name.strip("_").strip()
    name = name[:max_length]
    return name if name else "audio"


# ---------------------------------------------------------------------------
# clean_title
# ---------------------------------------------------------------------------

# Patterns to strip from YouTube titles — order matters (most specific first)
_CLEAN_PATTERNS: list[re.Pattern[str]] = [
    # Bracketed / parenthesised noise tags — case-insensitive
    re.compile(
        r"[\[\(]"
        r"(?:official\s*(?:video|mv|music\s*video|audio|lyric\s*video|lyrics?)?|"
        r"lyric\s*video|lyrics?|"
        r"(?:\d+[kK]\s*)?remastered?(?:\s*\d{4})?|"
        r"hq|hd|4k|"
        r"audio|"
        r"visualizer|"
        r"live(?:\s+version)?|"
        r"feat\.?[^)\]]*|"
        r"ft\.?[^)\]]*)"
        r"[\]\)]",
        re.IGNORECASE,
    ),
]

_TRAILING_DASH_RE = re.compile(r"[\s\-]+$")
_LEADING_DASH_RE = re.compile(r"^[\s\-]+")


def clean_title(title: str) -> str:
    """Light cleaning for YouTube titles used as Telegram audio title.

    - Remove patterns like [Official Video], (Official MV), (4K Remastered), [HQ], etc.
    - Strip leading/trailing whitespace and dashes
    - Preserve the actual title content
    """
    if not title:
        return ""
    for pattern in _CLEAN_PATTERNS:
        title = pattern.sub("", title)
    title = _TRAILING_DASH_RE.sub("", title)
    title = _LEADING_DASH_RE.sub("", title)
    return title.strip()
