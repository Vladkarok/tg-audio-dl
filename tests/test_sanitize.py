"""Tests for src/utils/sanitize.py — written first (TDD RED phase)."""

from src.utils.sanitize import clean_title, sanitize_filename


class TestSanitizeFilename:
    def test_sanitize_removes_special_chars(self):
        result = sanitize_filename("hello/world:test*file?")
        assert "/" not in result
        assert ":" not in result
        assert "*" not in result
        assert "?" not in result

    def test_sanitize_truncates_long_name(self):
        long_name = "a" * 200
        result = sanitize_filename(long_name, max_length=64)
        assert len(result) <= 64

    def test_sanitize_empty_string_fallback(self):
        result = sanitize_filename("")
        assert result == "audio"

    def test_sanitize_whitespace_only_fallback(self):
        result = sanitize_filename("   ")
        assert result == "audio"

    def test_sanitize_preserves_normal_name(self):
        result = sanitize_filename("My Song Title")
        assert result == "My Song Title"

    def test_sanitize_strips_leading_trailing_whitespace(self):
        result = sanitize_filename("  hello  ")
        assert result == "hello"

    def test_sanitize_collapses_multiple_underscores(self):
        result = sanitize_filename("hello###world")
        # ### becomes ___ which collapses to _
        assert "__" not in result

    def test_sanitize_preserves_allowed_chars(self):
        result = sanitize_filename("Track-01 (Live).mp3")
        assert result == "Track-01 (Live).mp3"

    def test_sanitize_replaces_unsafe_chars_with_underscore(self):
        result = sanitize_filename("a<b>c")
        assert "<" not in result
        assert ">" not in result


class TestCleanTitle:
    def test_clean_title_removes_official_video(self):
        result = clean_title("My Song [Official Video]")
        assert "[Official Video]" not in result
        assert "My Song" in result

    def test_clean_title_removes_official_mv(self):
        result = clean_title("My Song (Official MV)")
        assert "(Official MV)" not in result
        assert "My Song" in result

    def test_clean_title_removes_hq_tag(self):
        result = clean_title("My Song [HQ]")
        assert "[HQ]" not in result
        assert "My Song" in result

    def test_clean_title_removes_4k_remastered(self):
        result = clean_title("My Song (4K Remastered)")
        assert "4K Remastered" not in result
        assert "My Song" in result

    def test_clean_title_preserves_normal_title(self):
        result = clean_title("Bohemian Rhapsody")
        assert result == "Bohemian Rhapsody"

    def test_clean_title_empty_fallback(self):
        result = clean_title("")
        assert result == ""

    def test_clean_title_strips_whitespace(self):
        result = clean_title("  My Song  ")
        assert result == "My Song"

    def test_clean_title_removes_lyric_video_tag(self):
        result = clean_title("My Song (Lyric Video)")
        assert "Lyric Video" not in result

    def test_clean_title_removes_audio_tag(self):
        result = clean_title("My Song [Audio]")
        assert "[Audio]" not in result
        assert "My Song" in result
