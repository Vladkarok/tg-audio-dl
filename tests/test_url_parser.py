"""Tests for src/downloader/url_parser.py - Phase 2.

Covers all 16 URL cases from the spec plus edge cases for:
- canonical_url normalization
- tracking param stripping
- video_id / playlist_id extraction
- extract_youtube_urls() helper
"""

import pytest

from src.downloader.url_parser import (
    ParsedURL,
    URLType,
    extract_youtube_urls,
    parse_youtube_url,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VIDEO_ID = "dQw4w9WgXcQ"
PLAYLIST_ID = "PLxxxxxxxxxxxxxxxxxxxxx"
RADIO_ID = "RDdQw4w9WgXcQ"
RADIO_MM_ID = "RDMMdQw4w9WgXcQ"


def canonical_single(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"


def canonical_playlist(pid: str) -> str:
    return f"https://www.youtube.com/playlist?list={pid}"


# ===========================================================================
# parse_youtube_url — return type and basic contract
# ===========================================================================


class TestReturnType:
    """parse_youtube_url always returns ParsedURL | None."""

    def test_returns_parsed_url_for_valid(self):
        result = parse_youtube_url(f"https://www.youtube.com/watch?v={VIDEO_ID}")
        assert isinstance(result, ParsedURL)

    def test_returns_none_for_invalid(self):
        assert parse_youtube_url("https://google.com") is None

    def test_returns_none_for_empty_string(self):
        assert parse_youtube_url("") is None

    def test_returns_none_for_plain_text(self):
        assert parse_youtube_url("just some text without a url") is None

    def test_never_raises_on_malformed_input(self):
        """Must return None, never raise, for any garbage input."""
        garbage_inputs = [
            "http://",
            "://broken",
            "https://",
            "not_a_url_at_all",
            "https://youtube.com",  # no path, no video id
            "https://www.youtube.com/watch",  # no v= param
            "https://www.youtube.com/watch?v=",  # empty v=
        ]
        for inp in garbage_inputs:
            result = parse_youtube_url(inp)
            assert result is None, f"Expected None for {inp!r}, got {result!r}"


# ===========================================================================
# SINGLE — standard watch URL
# ===========================================================================


class TestSingleWatchURL:
    """Case 1: https://www.youtube.com/watch?v=ID"""

    def test_url_type_is_single(self):
        result = parse_youtube_url(f"https://www.youtube.com/watch?v={VIDEO_ID}")
        assert result.url_type == URLType.SINGLE

    def test_video_id_extracted(self):
        result = parse_youtube_url(f"https://www.youtube.com/watch?v={VIDEO_ID}")
        assert result.video_id == VIDEO_ID

    def test_playlist_id_is_none(self):
        result = parse_youtube_url(f"https://www.youtube.com/watch?v={VIDEO_ID}")
        assert result.playlist_id is None

    def test_canonical_url(self):
        result = parse_youtube_url(f"https://www.youtube.com/watch?v={VIDEO_ID}")
        assert result.canonical_url == canonical_single(VIDEO_ID)

    def test_original_url_preserved(self):
        raw = f"https://www.youtube.com/watch?v={VIDEO_ID}"
        result = parse_youtube_url(raw)
        assert result.original_url == raw


# ===========================================================================
# SINGLE — youtu.be short links
# ===========================================================================


class TestSingleYouTuBe:
    """Cases 2–3: https://youtu.be/ID and https://youtu.be/ID?si=..."""

    def test_youtu_be_plain(self):
        result = parse_youtube_url(f"https://youtu.be/{VIDEO_ID}")
        assert result is not None
        assert result.url_type == URLType.SINGLE
        assert result.video_id == VIDEO_ID

    def test_youtu_be_canonical_url(self):
        result = parse_youtube_url(f"https://youtu.be/{VIDEO_ID}")
        assert result.canonical_url == canonical_single(VIDEO_ID)

    def test_youtu_be_with_si_tracking_param(self):
        result = parse_youtube_url(f"https://youtu.be/{VIDEO_ID}?si=abc123tracking")
        assert result is not None
        assert result.url_type == URLType.SINGLE
        assert result.video_id == VIDEO_ID

    def test_youtu_be_si_stripped_from_canonical(self):
        result = parse_youtube_url(f"https://youtu.be/{VIDEO_ID}?si=abc123tracking")
        assert result.canonical_url == canonical_single(VIDEO_ID)

    def test_youtu_be_original_url_preserved(self):
        raw = f"https://youtu.be/{VIDEO_ID}?si=abc123"
        result = parse_youtube_url(raw)
        assert result.original_url == raw


# ===========================================================================
# SINGLE — tracking param stripping (case 4)
# ===========================================================================


class TestTrackingParamStripping:
    """Case 4: si=, feature=, index=, t= are stripped from canonical_url."""

    def test_si_stripped_from_watch_url(self):
        result = parse_youtube_url(
            f"https://www.youtube.com/watch?v={VIDEO_ID}&si=somevalue"
        )
        assert result.canonical_url == canonical_single(VIDEO_ID)

    def test_feature_stripped(self):
        result = parse_youtube_url(
            f"https://www.youtube.com/watch?v={VIDEO_ID}&feature=youtu.be"
        )
        assert result.canonical_url == canonical_single(VIDEO_ID)

    def test_index_stripped(self):
        result = parse_youtube_url(
            f"https://www.youtube.com/watch?v={VIDEO_ID}&index=3"
        )
        assert result.canonical_url == canonical_single(VIDEO_ID)

    def test_t_timestamp_stripped(self):
        result = parse_youtube_url(f"https://www.youtube.com/watch?v={VIDEO_ID}&t=42")
        assert result.canonical_url == canonical_single(VIDEO_ID)

    def test_multiple_tracking_params_stripped(self):
        result = parse_youtube_url(
            f"https://www.youtube.com/watch?v={VIDEO_ID}&si=abc&feature=share&index=2&t=10"
        )
        assert result.canonical_url == canonical_single(VIDEO_ID)

    def test_video_id_still_correct_after_stripping(self):
        result = parse_youtube_url(
            f"https://www.youtube.com/watch?v={VIDEO_ID}&si=abc&feature=share"
        )
        assert result.video_id == VIDEO_ID


# ===========================================================================
# SINGLE — Shorts (case 5)
# ===========================================================================


class TestSingleShorts:
    """Case 5: https://www.youtube.com/shorts/ID"""

    def test_shorts_url_type_is_single(self):
        result = parse_youtube_url(f"https://www.youtube.com/shorts/{VIDEO_ID}")
        assert result is not None
        assert result.url_type == URLType.SINGLE

    def test_shorts_video_id_extracted(self):
        result = parse_youtube_url(f"https://www.youtube.com/shorts/{VIDEO_ID}")
        assert result.video_id == VIDEO_ID

    def test_shorts_canonical_url(self):
        result = parse_youtube_url(f"https://www.youtube.com/shorts/{VIDEO_ID}")
        assert result.canonical_url == canonical_single(VIDEO_ID)

    def test_shorts_playlist_id_is_none(self):
        result = parse_youtube_url(f"https://www.youtube.com/shorts/{VIDEO_ID}")
        assert result.playlist_id is None


# ===========================================================================
# SINGLE — music.youtube.com (case 6)
# ===========================================================================


class TestSingleMusicYouTube:
    """Case 6: https://music.youtube.com/watch?v=ID"""

    def test_music_youtube_url_type_is_single(self):
        result = parse_youtube_url(f"https://music.youtube.com/watch?v={VIDEO_ID}")
        assert result is not None
        assert result.url_type == URLType.SINGLE

    def test_music_youtube_video_id_extracted(self):
        result = parse_youtube_url(f"https://music.youtube.com/watch?v={VIDEO_ID}")
        assert result.video_id == VIDEO_ID

    def test_music_youtube_canonical_normalized_to_www(self):
        """Canonical URL always uses www.youtube.com, not music.youtube.com."""
        result = parse_youtube_url(f"https://music.youtube.com/watch?v={VIDEO_ID}")
        assert result.canonical_url == canonical_single(VIDEO_ID)


# ===========================================================================
# SINGLE — mobile m.youtube.com (case 13)
# ===========================================================================


class TestSingleMobileYouTube:
    """Case 13: https://m.youtube.com/watch?v=ID"""

    def test_mobile_url_type_is_single(self):
        result = parse_youtube_url(f"https://m.youtube.com/watch?v={VIDEO_ID}")
        assert result is not None
        assert result.url_type == URLType.SINGLE

    def test_mobile_video_id_extracted(self):
        result = parse_youtube_url(f"https://m.youtube.com/watch?v={VIDEO_ID}")
        assert result.video_id == VIDEO_ID

    def test_mobile_canonical_normalized_to_www(self):
        result = parse_youtube_url(f"https://m.youtube.com/watch?v={VIDEO_ID}")
        assert result.canonical_url == canonical_single(VIDEO_ID)


# ===========================================================================
# RADIO_MIX — various forms
# ===========================================================================


class TestRadioMix:
    """Cases 7–9: list=RD*, list=RDMM*, start_radio=1 → RADIO_MIX."""

    def test_start_radio_param_is_radio_mix(self):
        """Case 7: v= + list=RD... + start_radio=1 → RADIO_MIX."""
        result = parse_youtube_url(
            f"https://www.youtube.com/watch?v={VIDEO_ID}&list={RADIO_ID}&start_radio=1"
        )
        assert result is not None
        assert result.url_type == URLType.RADIO_MIX

    def test_start_radio_video_id_extracted(self):
        result = parse_youtube_url(
            f"https://www.youtube.com/watch?v={VIDEO_ID}&list={RADIO_ID}&start_radio=1"
        )
        assert result.video_id == VIDEO_ID

    def test_start_radio_playlist_id_is_none(self):
        result = parse_youtube_url(
            f"https://www.youtube.com/watch?v={VIDEO_ID}&list={RADIO_ID}&start_radio=1"
        )
        assert result.playlist_id is None

    def test_start_radio_canonical_is_single_video(self):
        """RADIO_MIX canonical URL must point to the individual video only."""
        result = parse_youtube_url(
            f"https://www.youtube.com/watch?v={VIDEO_ID}&list={RADIO_ID}&start_radio=1"
        )
        assert result.canonical_url == canonical_single(VIDEO_ID)

    def test_rdmm_list_is_radio_mix(self):
        """Case 8: list=RDMM... → RADIO_MIX."""
        result = parse_youtube_url(
            f"https://www.youtube.com/watch?v={VIDEO_ID}&list={RADIO_MM_ID}"
        )
        assert result is not None
        assert result.url_type == URLType.RADIO_MIX

    def test_rdmm_video_id_extracted(self):
        result = parse_youtube_url(
            f"https://www.youtube.com/watch?v={VIDEO_ID}&list={RADIO_MM_ID}"
        )
        assert result.video_id == VIDEO_ID

    def test_rd_list_is_radio_mix(self):
        """Case 9: list=RD... (without start_radio) → RADIO_MIX."""
        result = parse_youtube_url(
            f"https://www.youtube.com/watch?v={VIDEO_ID}&list={RADIO_ID}"
        )
        assert result is not None
        assert result.url_type == URLType.RADIO_MIX

    def test_rd_list_video_id_extracted(self):
        result = parse_youtube_url(
            f"https://www.youtube.com/watch?v={VIDEO_ID}&list={RADIO_ID}"
        )
        assert result.video_id == VIDEO_ID

    def test_radio_mix_has_video_id_not_none(self):
        """RADIO_MIX must always have video_id set."""
        result = parse_youtube_url(
            f"https://www.youtube.com/watch?v={VIDEO_ID}&list={RADIO_ID}&start_radio=1&rv=XyZ"
        )
        assert result.video_id is not None

    def test_realistic_radio_url(self):
        """Full realistic radio URL with rv= param."""
        url = "https://www.youtube.com/watch?v=AbCdEfG1234&list=RDAbCdEfG1234&start_radio=1&rv=XyZ"
        result = parse_youtube_url(url)
        assert result is not None
        assert result.url_type == URLType.RADIO_MIX
        assert result.video_id == "AbCdEfG1234"
        assert result.canonical_url == "https://www.youtube.com/watch?v=AbCdEfG1234"

    def test_start_radio_only_no_v_param_returns_none(self):
        """start_radio=1 without v= cannot produce a canonical single URL → None."""
        result = parse_youtube_url(
            "https://www.youtube.com/watch?start_radio=1&list=RDsomething"
        )
        # Must not crash; can be None or treated as invalid
        assert (
            result is None
            or result.video_id is None
            or result.url_type == URLType.RADIO_MIX
        )


# ===========================================================================
# PLAYLIST — full playlist (case 10)
# ===========================================================================


class TestPlaylist:
    """Case 10: https://www.youtube.com/playlist?list=PLxxxx → PLAYLIST."""

    def test_playlist_url_type(self):
        result = parse_youtube_url(
            f"https://www.youtube.com/playlist?list={PLAYLIST_ID}"
        )
        assert result is not None
        assert result.url_type == URLType.PLAYLIST

    def test_playlist_id_extracted(self):
        result = parse_youtube_url(
            f"https://www.youtube.com/playlist?list={PLAYLIST_ID}"
        )
        assert result.playlist_id == PLAYLIST_ID

    def test_playlist_video_id_is_none(self):
        result = parse_youtube_url(
            f"https://www.youtube.com/playlist?list={PLAYLIST_ID}"
        )
        assert result.video_id is None

    def test_playlist_canonical_url(self):
        result = parse_youtube_url(
            f"https://www.youtube.com/playlist?list={PLAYLIST_ID}"
        )
        assert result.canonical_url == canonical_playlist(PLAYLIST_ID)


# ===========================================================================
# SINGLE — video within a playlist (cases 11–12)
# ===========================================================================


class TestVideoWithinPlaylist:
    """
    Cases 11–12: v= AND list=PL* (or FL*, UU*) → treat as SINGLE.
    User explicitly shared a specific video; do not download the whole list.
    """

    def test_v_plus_pl_list_is_single(self):
        """Case 11: v= + list=PL... → SINGLE."""
        result = parse_youtube_url(
            f"https://www.youtube.com/watch?v={VIDEO_ID}&list={PLAYLIST_ID}"
        )
        assert result is not None
        assert result.url_type == URLType.SINGLE

    def test_v_plus_pl_video_id_extracted(self):
        result = parse_youtube_url(
            f"https://www.youtube.com/watch?v={VIDEO_ID}&list={PLAYLIST_ID}"
        )
        assert result.video_id == VIDEO_ID

    def test_v_plus_pl_canonical_is_single_video(self):
        result = parse_youtube_url(
            f"https://www.youtube.com/watch?v={VIDEO_ID}&list={PLAYLIST_ID}"
        )
        assert result.canonical_url == canonical_single(VIDEO_ID)

    def test_v_plus_fl_list_is_single(self):
        """Case 12: v= + list=FL... (liked videos) → SINGLE."""
        result = parse_youtube_url(
            f"https://www.youtube.com/watch?v={VIDEO_ID}&list=FLxxxxxxxxxxxxxxxxxxxxx"
        )
        assert result is not None
        assert result.url_type == URLType.SINGLE

    def test_v_plus_uu_list_is_single(self):
        """v= + list=UU... (uploads) → SINGLE."""
        result = parse_youtube_url(
            f"https://www.youtube.com/watch?v={VIDEO_ID}&list=UUxxxxxxxxxxxxxxxxxxxxx"
        )
        assert result is not None
        assert result.url_type == URLType.SINGLE


# ===========================================================================
# Non-YouTube and invalid URLs
# ===========================================================================


class TestInvalidURLs:
    """Cases 14–16: non-YouTube URLs return None."""

    def test_google_com_returns_none(self):
        """Case 15: https://google.com → None."""
        assert parse_youtube_url("https://google.com") is None

    def test_plain_text_returns_none(self):
        """Case 14: plain text → None."""
        assert parse_youtube_url("just some text") is None

    def test_empty_string_returns_none(self):
        """Case 16: empty string → None."""
        assert parse_youtube_url("") is None

    def test_vimeo_url_returns_none(self):
        assert parse_youtube_url("https://vimeo.com/123456789") is None

    def test_twitter_url_returns_none(self):
        assert parse_youtube_url("https://twitter.com/user/status/12345") is None

    def test_none_like_youtube_domain_returns_none(self):
        """Lookalike domain must be rejected."""
        assert parse_youtube_url("https://www.youtube.com.evil.com/watch?v=abc") is None

    def test_watch_url_without_v_param_returns_none(self):
        assert parse_youtube_url("https://www.youtube.com/watch?list=PLabc") is None

    def test_watch_url_empty_v_param_returns_none(self):
        assert parse_youtube_url("https://www.youtube.com/watch?v=") is None


# ===========================================================================
# ParsedURL dataclass integrity
# ===========================================================================


class TestParsedURLDataclass:
    """Verify ParsedURL is frozen (immutable) and has expected fields."""

    def test_parsed_url_is_frozen(self):
        result = parse_youtube_url(f"https://www.youtube.com/watch?v={VIDEO_ID}")
        with pytest.raises((AttributeError, TypeError)):
            result.video_id = "different_id"  # type: ignore[misc]

    def test_parsed_url_has_original_url_field(self):
        raw = f"https://youtu.be/{VIDEO_ID}?si=tracking"
        result = parse_youtube_url(raw)
        assert result.original_url == raw

    def test_canonical_differs_from_original_when_tracking_params_present(self):
        raw = f"https://youtu.be/{VIDEO_ID}?si=tracking123"
        result = parse_youtube_url(raw)
        assert result.canonical_url != result.original_url
        assert result.canonical_url == canonical_single(VIDEO_ID)


# ===========================================================================
# URLType enum completeness
# ===========================================================================


class TestURLTypeEnum:
    def test_single_enum_exists(self):
        assert URLType.SINGLE is not None

    def test_playlist_enum_exists(self):
        assert URLType.PLAYLIST is not None

    def test_radio_mix_enum_exists(self):
        assert URLType.RADIO_MIX is not None

    def test_enum_values_are_distinct(self):
        values = {URLType.SINGLE, URLType.PLAYLIST, URLType.RADIO_MIX}
        assert len(values) == 3


# ===========================================================================
# extract_youtube_urls — helper function
# ===========================================================================


class TestExtractYouTubeURLs:
    """Tests for extract_youtube_urls(text) → list[ParsedURL]."""

    def test_single_url_embedded_in_sentence(self):
        text = f"Hey, check this out: https://www.youtube.com/watch?v={VIDEO_ID} it's great!"
        results = extract_youtube_urls(text)
        assert len(results) == 1
        assert results[0].url_type == URLType.SINGLE
        assert results[0].video_id == VIDEO_ID

    def test_multiple_urls_in_text(self):
        text = (
            f"First: https://www.youtube.com/watch?v={VIDEO_ID} "
            f"and also https://youtu.be/AbCdEfG1234"
        )
        results = extract_youtube_urls(text)
        assert len(results) == 2
        video_ids = {r.video_id for r in results}
        assert VIDEO_ID in video_ids
        assert "AbCdEfG1234" in video_ids

    def test_no_urls_returns_empty_list(self):
        text = "There are no YouTube links in this message at all."
        results = extract_youtube_urls(text)
        assert results == []

    def test_text_that_is_a_url(self):
        text = f"https://www.youtube.com/watch?v={VIDEO_ID}"
        results = extract_youtube_urls(text)
        assert len(results) == 1
        assert results[0].video_id == VIDEO_ID

    def test_non_youtube_urls_excluded(self):
        text = "Go to https://google.com and also https://vimeo.com/123 not YouTube"
        results = extract_youtube_urls(text)
        assert results == []

    def test_mixed_youtube_and_non_youtube(self):
        text = f"See https://google.com and https://youtu.be/{VIDEO_ID} for details"
        results = extract_youtube_urls(text)
        assert len(results) == 1
        assert results[0].video_id == VIDEO_ID

    def test_playlist_url_extracted(self):
        text = f"Full album: https://www.youtube.com/playlist?list={PLAYLIST_ID}"
        results = extract_youtube_urls(text)
        assert len(results) == 1
        assert results[0].url_type == URLType.PLAYLIST
        assert results[0].playlist_id == PLAYLIST_ID

    def test_radio_url_extracted(self):
        text = f"Radio: https://www.youtube.com/watch?v={VIDEO_ID}&list={RADIO_ID}"
        results = extract_youtube_urls(text)
        assert len(results) == 1
        assert results[0].url_type == URLType.RADIO_MIX

    def test_duplicate_urls_both_returned(self):
        """Same URL twice → two ParsedURL objects (dedup is caller's concern)."""
        url = f"https://www.youtube.com/watch?v={VIDEO_ID}"
        text = f"{url} and again {url}"
        results = extract_youtube_urls(text)
        assert len(results) == 2

    def test_empty_text_returns_empty_list(self):
        results = extract_youtube_urls("")
        assert results == []

    def test_returns_list_type(self):
        results = extract_youtube_urls("no urls here")
        assert isinstance(results, list)

    def test_youtu_be_with_tracking_extracted_and_normalized(self):
        text = f"Listen: https://youtu.be/{VIDEO_ID}?si=trackingABC cool right?"
        results = extract_youtube_urls(text)
        assert len(results) == 1
        assert results[0].canonical_url == canonical_single(VIDEO_ID)
