# Plan: YouTube Chapters in Telegram Audio Caption

## Overview

Extract chapter/timestamp data from YouTube video metadata (yt-dlp `chapters` key)
and include in the Telegram audio caption. Chapters persist through cache via M4A
embedded metadata (yt-dlp already sets `embedchapters: True`).

## Caption Format

```
🎵 Video Title

00:00:00 Introduction
00:23:37 Chapter 1
00:49:24 Chapter 2
...
```

## Architecture

### Data Flow

```
yt-dlp download (embedchapters: True)
  → chapters embedded in M4A file (chpl atom)
  → also extracted from info_dict into DownloadResult.chapters

Fresh download path:
  info_dict["chapters"] → DownloadResult.chapters → _build_caption() → send_audio

Cache hit path:
  M4A file → mutagen MP4.chapters → DownloadResult.chapters → _build_caption() → send_audio
```

### Key Insight

yt-dlp already has `embedchapters: True` in the config (`client.py:327`).
Mutagen can read chapters from M4A files via `MP4(path).chapters`.
This means chapters survive cache round-trips without any extra sidecar files.

## Implementation Phases

### Phase 1: Data Model (`src/downloader/client.py`)

1. Add type alias: `Chapter = tuple[int, str]` (start_seconds, title)
2. Add field to `DownloadResult`: `chapters: tuple[Chapter, ...] | None`
3. Populate in `_build_result()` from `info.get("chapters")`

### Phase 2: Caption Formatting (`src/bot/handlers.py`)

4. Add `_format_chapters(chapters) -> str` — converts to `HH:MM:SS Title` lines
5. Add `_build_caption(title, chapters, max_length=1024) -> str`:
   - Title line + `\n\n` + chapter lines
   - Truncation: drop chapters from bottom, append `\n...`
   - If <2 chapters fit, show title only
6. Wire into `_send_audio()`: replace `caption=f"🎵 {display_title}"`

### Phase 3: Cache Hit Chapter Extraction (`src/bot/handlers.py`)

7. Update `_extract_m4a_metadata()` to also return chapters from `MP4.chapters`
8. Pass chapters into `DownloadResult` in cache-hit construction
9. Both fresh and cached paths now show chapters

### Phase 4: Fix Construction Sites

10. All `DownloadResult(...)` calls need `chapters=` parameter
11. Update test helper `make_download_result()`

### Phase 5: Tests

12. Chapter extraction from yt-dlp info_dict (present/absent/empty)
13. Chapter extraction from M4A metadata via mutagen
14. `_format_chapters()` time formatting
15. `_build_caption()` normal + truncation at 1024 chars
16. Integration: `bot.send_audio` receives correct caption

## Truncation Strategy

```python
title_line = f"🎵 {display_title}"
if no chapters → return title_line

full = title_line + "\n\n" + chapter_lines
if len(full) <= 1024 → return full

# Drop chapters from bottom, add "\n..." suffix
# If <2 chapters fit after title → return title_line only
```

## Risks & Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| yt-dlp changes chapters format | Low | Defensive `.get()` + `None` fallback |
| mutagen can't read embedded chapters | Medium | Verify with real file; fallback to None |
| Truncation looks awkward with 1 chapter | Medium | Show title-only if <2 chapters fit |
| M4A chapter embedding fails silently | Medium | Fresh download always has info_dict chapters as primary source |

## Success Criteria

- [ ] YouTube videos with chapters show timestamps in caption
- [ ] Videos without chapters show title-only (no regression)
- [ ] SoundCloud tracks unaffected
- [ ] Cache hits preserve and display chapters
- [ ] Caption never exceeds 1024 characters
- [ ] Truncation adds `...` indicator
- [ ] All tests pass, 80%+ coverage maintained
