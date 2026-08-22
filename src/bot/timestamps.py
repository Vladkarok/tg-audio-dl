"""Parser for hand-entered chapter timestamps.

Input is a block of text pasted by a curator, usually copied straight out of a
YouTube comment. Unlike yt-dlp's chapter data this is untrusted, unstructured,
and impossible to re-derive if it is stored wrong, so parsing rejects anything
ambiguous with a line-numbered error instead of silently repairing it.

Accepted per line (leading and trailing timestamp forms, both common in
comments)::

    0:00 Intro
    00:00 - Intro
    [1:02:10] Closer
    Closer 1:02:10

Blank lines are skipped. Everything else is an error.
"""

from __future__ import annotations

import re
import unicodedata

# (start_seconds, title)
Chapter = tuple[int, str]

# Limits. Total volume is bounded by MAX_INPUT_CHARS and MAX_CHAPTERS; the
# per-title cap exists because one entry has to fit on a chapter page by itself.
# The hard ceiling there is _CHAPTER_PAGE_BODY_MAX (970) minus the timestamp
# prefix: overshoot it and _build_chapter_pages gives up, killing pagination for
# the whole track rather than just that line. 300 leaves room for three entries
# per page while still accepting the most verbose real tracklist entries
# ("Artist feat. Someone - Title (Extended Club Mix) [Label 2019]" and longer).
MAX_CHAPTERS = 300
MAX_TITLE_CHARS = 300
MAX_INPUT_CHARS = 20_000

# 1:23 / 01:23 / 1:02:10 / 01:02:10. The hour part is optional; without it the
# minute field may run past 59, because a two-hour mix is often timestamped
# "127:30" rather than "2:07:30".
_TS = r"(?:(?P<h>\d{1,3}):)?(?P<m>\d{1,3}):(?P<s>\d{2})"
_LEADING_RE = re.compile(
    rf"^[\[\(\-–—•*]*\s*{_TS}\s*[\]\)]*\s*[-–—:.|]?\s*(?P<title>.+)$"
)
_TRAILING_RE = re.compile(rf"^(?P<title>.+?)\s*[-–—:.|]?\s*[\[\(]*\s*{_TS}\s*[\]\)]*$")
# Any timestamp-shaped token, used to count them per line. A line carrying two
# is ambiguous no matter where they sit, so counting beats matching the
# leading/trailing forms against each other.
_TS_TOKEN_RE = re.compile(r"(?<!\d)\d{1,3}:\d{2}(?::\d{2})?(?!\d)")

# A line that is nothing but a timestamp: worth its own message, since "no
# timestamp found" would send the curator looking for the wrong problem.
_BARE_RE = re.compile(rf"^[\[\(\-–—•*]*\s*{_TS}\s*[\]\)]*\s*[-–—:.|]?\s*$")


class TimestampParseError(ValueError):
    """Raised with a user-facing, line-numbered message."""


def _clean_title(raw: str) -> str:
    """Strip decoration and invisible characters from a chapter title.

    Unicode category C covers zero-width joiners, bidi overrides, and other
    format controls that survive .strip() and can reorder or hide text once the
    title is rendered into a caption.
    """
    title = "".join(ch for ch in raw if not unicodedata.category(ch).startswith("C"))
    # Drop separator punctuation left over from "0:00 - Intro" style lines.
    title = title.strip().strip("-–—:.|").strip()
    return re.sub(r"\s{2,}", " ", title)


def _seconds(match: re.Match[str]) -> int:
    hours = int(match.group("h") or 0)
    minutes = int(match.group("m"))
    seconds = int(match.group("s"))
    if seconds > 59:
        raise TimestampParseError("seconds must be 00-59")
    if hours and minutes > 59:
        raise TimestampParseError("minutes must be 00-59 when hours are given")
    return hours * 3600 + minutes * 60 + seconds


def _parse_line(line: str) -> Chapter:
    """Parse one non-blank line into a chapter, or raise TimestampParseError."""
    if _BARE_RE.match(line):
        raise TimestampParseError("timestamp has no title")

    # More than one timestamp on a line is ambiguous ("1:00 track sampled at
    # 2:00", "Song 1:00 remix 2:00"): guessing would silently file the entry
    # under the wrong start time, and one wrong start shifts nothing else, so
    # it is easy to miss in the rendered list.
    if len(_TS_TOKEN_RE.findall(line)) > 1:
        raise TimestampParseError(
            "two timestamps on one line — keep one, at the start or the end"
        )

    match = _LEADING_RE.match(line) or _TRAILING_RE.match(line)
    if match is None:
        raise TimestampParseError("no timestamp found")

    title = _clean_title(match.group("title"))
    if not title:
        raise TimestampParseError("timestamp has no title")
    if len(title) > MAX_TITLE_CHARS:
        raise TimestampParseError(f"title longer than {MAX_TITLE_CHARS} characters")

    return _seconds(match), title


def parse_timestamps(
    text: str, duration_seconds: int | None = None
) -> tuple[Chapter, ...]:
    """Parse a pasted timestamp block into chapters.

    Starts must be strictly increasing: a list that jumps backwards is a paste
    of two different tracklists, or has a typo, and either way the curator
    should see it rather than have it silently sorted.

    When *duration_seconds* is known, timestamps past the end are rejected —
    the usual cause is a tracklist pasted onto the wrong video.

    Raises TimestampParseError with a message meant for the user.
    """
    if len(text) > MAX_INPUT_CHARS:
        raise TimestampParseError(
            f"Input too long ({len(text)} characters, limit {MAX_INPUT_CHARS})."
        )

    chapters: list[Chapter] = []
    errors: list[str] = []

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            start, title = _parse_line(line)
        except TimestampParseError as exc:
            errors.append(f"line {lineno}: {exc}")
            continue

        if chapters and start <= chapters[-1][0]:
            errors.append(
                f"line {lineno}: {_format_hms(start)} is not after "
                f"{_format_hms(chapters[-1][0])}"
            )
            continue
        if duration_seconds is not None and start > duration_seconds:
            errors.append(
                f"line {lineno}: {_format_hms(start)} is past the end of the track "
                f"({_format_hms(duration_seconds)})"
            )
            continue

        chapters.append((start, title))

    if errors:
        shown = errors[:5]
        if len(errors) > len(shown):
            shown.append(f"…and {len(errors) - len(shown)} more")
        raise TimestampParseError("Could not parse:\n" + "\n".join(shown))

    if not chapters:
        raise TimestampParseError("No timestamps found.")
    if len(chapters) > MAX_CHAPTERS:
        raise TimestampParseError(
            f"Too many chapters ({len(chapters)}, limit {MAX_CHAPTERS})."
        )

    return tuple(chapters)


def _format_hms(seconds: int) -> str:
    """Format seconds as M:SS or H:MM:SS (error messages only)."""
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
