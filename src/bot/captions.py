"""Caption building for audio messages: overflow tiers and chapter pages.

Pure functions only — no Telegram I/O happens here. Handlers compose these
into send_audio calls and callback-query edits.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.cache.base import validate_video_id
from src.downloader.client import Chapter
from src.utils.sanitize import clean_title

_CAPTION_MAX = 1024
_MESSAGE_MAX = 4096


@dataclass(frozen=True)
class CaptionResult:
    """Caption for send_audio plus optional chapter-index reply messages."""

    caption: str
    # Each element is one reply message (≤4096 chars). Empty = no follow-up needed.
    index_messages: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ChapterPage:
    """One experimental caption page for chapter navigation."""

    caption: str
    label: str


@dataclass(frozen=True)
class AudioMessage:
    """How to render one audio message's chapters in the normal send path.

    Exactly one overflow strategy is active:

    - ``reply_markup`` set → paginated chapter pages (``caption`` is page 1).
    - ``index_messages`` set → four-tier index-message follow-ups.
    - both empty → the caption fit, nothing more to send.
    """

    caption: str
    reply_markup: InlineKeyboardMarkup | None = None
    index_messages: tuple[str, ...] = field(default_factory=tuple)


def _format_timestamp(seconds: int) -> str:
    """Format seconds as a compact Telegram media timestamp.

    Keeps exact second precision while omitting redundant leading hours/zeroes:
    ``0:45``, ``2:05``, ``1:01:01``.
    """
    h, remainder = divmod(seconds, 3600)
    m, s = divmod(remainder, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _normalize_chapters(chapters: tuple[Chapter, ...]) -> tuple[Chapter, ...]:
    """Strip whitespace, drop empty names, deduplicate start times (first wins)."""
    seen: set[int] = set()
    result: list[Chapter] = []
    for start, name in chapters:
        clean = " ".join(name.split())
        if not clean or start in seen:
            continue
        seen.add(start)
        result.append((start, clean))
    return tuple(result)


def _build_index_messages(
    chapters: tuple[Chapter, ...],
    header: str,
    include_timestamps: bool = False,
    max_length: int = _MESSAGE_MAX,
) -> tuple[str, ...]:
    """Pack chapter index lines into one or more messages each ≤ max_length chars.

    When include_timestamps is True each line is: ``HH:MM:SS N - Name``
    Otherwise: ``N - Name``
    """
    if include_timestamps:
        lines = [
            f"{_format_timestamp(s)} {i} - {name}"
            for i, (s, name) in enumerate(chapters, 1)
        ]
    else:
        lines = [f"{i} - {name}" for i, (_, name) in enumerate(chapters, 1)]

    messages: list[str] = []
    current: list[str] = [header]
    current_len = len(header)

    for line in lines:
        needed = 1 + len(line)  # leading \n
        if current_len + needed > max_length and current:
            messages.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += needed

    if current:
        messages.append("\n".join(current))

    return tuple(messages)


def _build_caption_result(
    title: str,
    chapters: tuple[Chapter, ...] | None,
) -> CaptionResult:
    """Build caption + optional chapter-index follow-up messages.

    Four-tier strategy (timestamps are NEVER individually truncated):

    Tier 1  Full caption (title + full chapter names) fits in 1024 chars.
            → Caption only, no follow-up.

    Tier 2  Full names don't fit; title + numbered timestamps fit.
            → Caption: ``🎵 Title\\n\\nHH:MM:SS 1\\n...``
            → Follow-up: ``📋 Chapters:\\n1 - Name\\n...``

    Tier 3  Title + numbered timestamps don't fit; numbered timestamps alone fit.
            → Caption: ``HH:MM:SS 1\\n...`` (no title)
            → Follow-up includes title header so user can still see it.

    Tier 4  Even bare numbered timestamps exceed 1024 (200+ chapters).
            → Caption: ``🎵 Title`` only (no timestamps at all).
            → Follow-up: ``🎵 Title\\n\\n📋 All chapters:\\nHH:MM:SS 1 - Name\\n...``
              so all navigation info is available in the reply.
    """
    title_line = f"🎵 {title}"

    if not chapters:
        return CaptionResult(caption=title_line[:_CAPTION_MAX])

    chapters = _normalize_chapters(chapters)
    if not chapters:
        return CaptionResult(caption=title_line[:_CAPTION_MAX])

    # --- Tier 1: full caption with chapter names ---
    full_lines = [f"{_format_timestamp(s)} {name}" for s, name in chapters]
    full_caption = title_line + "\n\n" + "\n".join(full_lines)
    if len(full_caption) <= _CAPTION_MAX:
        return CaptionResult(caption=full_caption)

    # --- Tier 2: numbered timestamps + title ---
    numbered_lines = [
        f"{_format_timestamp(s)} {i}" for i, (s, _) in enumerate(chapters, 1)
    ]
    tier2_caption = title_line + "\n\n" + "\n".join(numbered_lines)
    if len(tier2_caption) <= _CAPTION_MAX:
        index = _build_index_messages(chapters, header="📋 Chapters:")
        return CaptionResult(caption=tier2_caption, index_messages=index)

    # --- Tier 3: numbered timestamps only (no title) ---
    tier3_caption = "\n".join(numbered_lines)
    if len(tier3_caption) <= _CAPTION_MAX:
        header = f"🎵 {title}\n\n📋 Chapters:"
        index = _build_index_messages(chapters, header=header)
        return CaptionResult(caption=tier3_caption, index_messages=index)

    # --- Tier 4: title-only caption; all info in follow-up (with timestamps) ---
    header = f"🎵 {title}\n\n📋 All chapters:"
    index = _build_index_messages(chapters, header=header, include_timestamps=True)
    return CaptionResult(caption=title_line[:_CAPTION_MAX], index_messages=index)


_CHAPTER_PAGE_CALLBACK_PREFIX = "cp"
_CHAPTER_PAGE_BODY_MAX = 970


def _chapter_page_header(title: str, page_number: int, total: int, label: str) -> str:
    return f"🎵 {title}\n{page_number}/{total} · {label}"


def _extract_chapter_page_title(caption: str | None) -> str | None:
    if not caption:
        return None
    first_line = caption.splitlines()[0].strip()
    if not first_line.startswith("🎵 "):
        return None
    title = first_line.removeprefix("🎵 ").strip()
    return title or None


def _chapter_page_callback_data(video_id: str, page_index: int) -> str | None:
    data = f"{_CHAPTER_PAGE_CALLBACK_PREFIX}:{video_id}:{page_index}"
    # Bot API callback_data is 1-64 bytes, measured after UTF-8 encoding.
    return data if len(data.encode()) <= 64 else None


def _parse_chapter_page_callback_data(data: str) -> tuple[str, int] | None:
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != _CHAPTER_PAGE_CALLBACK_PREFIX:
        return None
    video_id = parts[1]
    try:
        page_index = int(parts[2])
    except ValueError:
        return None
    if page_index < 0:
        return None
    try:
        validate_video_id(video_id)
    except ValueError:
        return None
    return video_id, page_index


def _pack_chapter_page_entries(
    entries: list[tuple[int, str]],
    body_max: int,
) -> list[tuple[int, int, list[str]]] | None:
    raw_pages: list[tuple[int, int, list[str]]] = []
    current_start = entries[0][0]
    current_lines: list[str] = []
    current_len = 0

    for index, line in entries:
        if len(line) > body_max:
            return None
        needed = len(line) if not current_lines else 1 + len(line)
        if current_lines and current_len + needed > body_max:
            raw_pages.append((current_start, entries[index - 2][0], current_lines))
            current_start = index
            current_lines = [line]
            current_len = len(line)
        else:
            current_lines.append(line)
            current_len += needed

    raw_pages.append((current_start, entries[-1][0], current_lines))
    return raw_pages


def _build_chapter_pages(
    title: str,
    chapters: tuple[Chapter, ...],
) -> tuple[ChapterPage, ...]:
    """Build compact, lossless chapter caption pages for inline navigation."""
    normalized = _normalize_chapters(chapters)
    if not normalized:
        return ()
    # Collapse to a single line: the page header puts the title on its own line
    # and the callback handler reconstructs it from that first line, so an
    # embedded newline would desync the rebuilt pages from the sent ones.
    title = " ".join((clean_title(title) or "Untitled").split()) or "Untitled"

    entries = [
        (index, f"{_format_timestamp(start)} - {name}")
        for index, (start, name) in enumerate(normalized, start=1)
    ]

    body_max = _CHAPTER_PAGE_BODY_MAX
    raw_pages: list[tuple[int, int, list[str]]] | None = None
    for _ in range(len(entries)):
        raw_pages = _pack_chapter_page_entries(entries, body_max)
        if raw_pages is None:
            return ()

        total = len(raw_pages)
        available = _CHAPTER_PAGE_BODY_MAX
        for page_number, (start, end, _lines) in enumerate(raw_pages, start=1):
            label = f"{start:02d}-{end:02d}"
            header = _chapter_page_header(title, page_number, total, label)
            available = min(available, _CAPTION_MAX - len(header) - 1)
        if available < 1:
            return ()
        if available == body_max:
            break
        body_max = available
    else:
        return ()

    total = len(raw_pages)
    pages: list[ChapterPage] = []
    for page_number, (start, end, lines) in enumerate(raw_pages, start=1):
        label = f"{start:02d}-{end:02d}"
        caption = _chapter_page_header(title, page_number, total, label)
        caption += "\n" + "\n".join(lines)
        if len(caption) > _CAPTION_MAX:
            return ()
        pages.append(ChapterPage(caption=caption, label=label))

    return tuple(pages)


def _build_chapter_pages_markup(
    video_id: str,
    pages: tuple[ChapterPage, ...],
) -> InlineKeyboardMarkup | None:
    """Build page buttons, returning None when callback data cannot fit."""
    if len(pages) <= 1:
        return None

    buttons: list[InlineKeyboardButton] = []
    for index, page in enumerate(pages):
        data = _chapter_page_callback_data(video_id, index)
        if data is None:
            return None
        buttons.append(InlineKeyboardButton(page.label, callback_data=data))

    rows = [buttons[i : i + 5] for i in range(0, len(buttons), 5)]
    return InlineKeyboardMarkup(rows)


def _build_audio_message(
    title: str,
    chapters: tuple[Chapter, ...] | None,
    video_id: str | None,
    *,
    paginate: bool,
) -> AudioMessage:
    """Compose the caption (and any chapter overflow UI) for an audio message.

    - Chapters fit in the 1024-char caption → caption only, no follow-up.
    - Overflow + ``paginate`` + pages buildable → first chapter page with inline
      navigation buttons (``_build_chapter_pages`` / ``_build_chapter_pages_markup``).
    - Overflow otherwise → the four-tier ``_build_caption_result`` index messages.

    The pages path needs ``video_id`` for callback data; when it is missing or the
    pages cannot be packed (e.g. callback key too long), it degrades to the
    four-tier fallback so no message is ever sent with dead buttons.
    """
    result = _build_caption_result(title, chapters)

    # No overflow (or no chapters): the caption already says everything.
    if not result.index_messages:
        return AudioMessage(caption=result.caption)

    # Overflow: prefer paginated pages when enabled and constructible.
    if paginate and chapters and video_id:
        pages = _build_chapter_pages(title, chapters)
        if pages:
            markup = _build_chapter_pages_markup(video_id, pages)
            if len(pages) == 1 or markup is not None:
                return AudioMessage(caption=pages[0].caption, reply_markup=markup)

    # Fallback: original four-tier index-message behavior.
    return AudioMessage(caption=result.caption, index_messages=result.index_messages)
