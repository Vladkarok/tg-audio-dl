"""Tests for src/bot/filters.py — written first (TDD RED phase)."""

from unittest.mock import MagicMock

from src.bot.filters import MediaURLFilter, YouTubeURLFilter


def make_message(text):
    """Create a minimal mock Message with a text attribute."""
    msg = MagicMock()
    msg.text = text
    return msg


class TestYouTubeURLFilter:
    def setup_method(self):
        self.f = YouTubeURLFilter()

    def test_filter_matches_youtube_url(self):
        msg = make_message("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert self.f.filter(msg) is True

    def test_filter_matches_youtu_be_url(self):
        msg = make_message("https://youtu.be/dQw4w9WgXcQ")
        assert self.f.filter(msg) is True

    def test_filter_matches_youtube_url_in_sentence(self):
        msg = make_message("Check this out https://youtu.be/dQw4w9WgXcQ awesome")
        assert self.f.filter(msg) is True

    def test_filter_rejects_plain_text(self):
        msg = make_message("Hello, how are you?")
        assert self.f.filter(msg) is False

    def test_filter_rejects_other_url(self):
        msg = make_message("https://vimeo.com/123456789")
        assert self.f.filter(msg) is False

    def test_filter_rejects_empty_message(self):
        msg = make_message("")
        assert self.f.filter(msg) is False

    def test_filter_rejects_none_text(self):
        msg = make_message(None)
        assert self.f.filter(msg) is False

    def test_filter_matches_shorts_url(self):
        msg = make_message("https://www.youtube.com/shorts/dQw4w9WgXcQ")
        assert self.f.filter(msg) is True

    def test_filter_matches_playlist_url(self):
        msg = make_message(
            "https://www.youtube.com/playlist?list=PLrEnWoR732-BHrPp_Pm8_VleD68f9s14-"
        )
        assert self.f.filter(msg) is True

    def test_filter_matches_soundcloud_track(self):
        msg = make_message("https://soundcloud.com/rick-astley/never-gonna-give-you-up")
        assert self.f.filter(msg) is True

    def test_filter_matches_soundcloud_set(self):
        msg = make_message("https://soundcloud.com/rick-astley/sets/greatest-hits")
        assert self.f.filter(msg) is True

    def test_filter_matches_on_soundcloud_short_url(self):
        msg = make_message("https://on.soundcloud.com/AbCdEf123")
        assert self.f.filter(msg) is True


class TestMediaURLFilter:
    """MediaURLFilter is the canonical name; YouTubeURLFilter is the legacy alias."""

    def setup_method(self):
        self.f = MediaURLFilter()

    def test_matches_youtube(self):
        msg = make_message("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert self.f.filter(msg) is True

    def test_matches_soundcloud(self):
        msg = make_message("https://soundcloud.com/artist/track")
        assert self.f.filter(msg) is True

    def test_rejects_unrelated(self):
        msg = make_message("https://vimeo.com/123")
        assert self.f.filter(msg) is False
