"""Tests for src/downloader/client.py — Phase 4 TDD suite.

All yt-dlp I/O is mocked; no real network calls are made.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.downloader.client import (
    AudioDownloader,
    DownloadError,
    DownloadProgress,
    DownloadResult,
    FileTooLargeError,
    VideoUnavailableError,
)
from src.downloader.url_parser import ParsedURL, URLType

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VIDEO_ID = "dQw4w9WgXcQ"
PLAYLIST_ID = "PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"

FAKE_SINGLE_INFO: dict = {
    "id": VIDEO_ID,
    "title": "Never Gonna Give You Up",
    "uploader": "Rick Astley",
    "channel": "RickAstleyVEVO",
    "duration": 213,
    "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg",
    "ext": "m4a",
    "requested_downloads": [{"filepath": None}],  # patched per-test
}

FAKE_PLAYLIST_ENTRIES: list[dict] = [
    {
        "id": "aaaaaaaaaaa",
        "title": "Track 1",
        "uploader": "Artist A",
        "channel": "ChannelA",
        "duration": 180,
        "thumbnail": "https://example.com/thumb1.jpg",
        "ext": "m4a",
    },
    {
        "id": "bbbbbbbbbbb",
        "title": "Track 2",
        "uploader": "Artist B",
        "channel": "ChannelB",
        "duration": 240,
        "thumbnail": "https://example.com/thumb2.jpg",
        "ext": "m4a",
    },
    {
        "id": "ccccccccccc",
        "title": "Track 3",
        "uploader": "Artist C",
        "channel": "ChannelC",
        "duration": 200,
        "thumbnail": "https://example.com/thumb3.jpg",
        "ext": "m4a",
    },
]

FAKE_PLAYLIST_INFO: dict = {
    "id": PLAYLIST_ID,
    "title": "My Playlist",
    "_type": "playlist",
    "entries": FAKE_PLAYLIST_ENTRIES,
}


def _make_parsed_single(video_id: str = VIDEO_ID) -> ParsedURL:
    return ParsedURL(
        url_type=URLType.SINGLE,
        video_id=video_id,
        playlist_id=None,
        canonical_url=f"https://www.youtube.com/watch?v={video_id}",
        original_url=f"https://www.youtube.com/watch?v={video_id}",
    )


def _make_parsed_radio_mix(video_id: str = VIDEO_ID) -> ParsedURL:
    return ParsedURL(
        url_type=URLType.RADIO_MIX,
        video_id=video_id,
        playlist_id=None,
        canonical_url=f"https://www.youtube.com/watch?v={video_id}",
        original_url=f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}",
    )


def _make_parsed_playlist(playlist_id: str = PLAYLIST_ID) -> ParsedURL:
    return ParsedURL(
        url_type=URLType.PLAYLIST,
        video_id=None,
        playlist_id=playlist_id,
        canonical_url=f"https://www.youtube.com/playlist?list={playlist_id}",
        original_url=f"https://www.youtube.com/playlist?list={playlist_id}",
    )


def _create_fake_m4a(directory: Path, video_id: str, size_bytes: int = 1024) -> Path:
    """Create a fake .m4a file in *directory* for *video_id*."""
    p = directory / f"{video_id}.m4a"
    p.write_bytes(b"x" * size_bytes)
    return p


def _make_ydl_mock(info_dict: dict, captured_opts: list | None = None) -> MagicMock:
    """Return a MagicMock that mimics YoutubeDL context-manager usage."""
    mock_ydl = MagicMock()
    mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
    mock_ydl.__exit__ = MagicMock(return_value=False)
    mock_ydl.extract_info.return_value = info_dict

    if captured_opts is not None:
        # Capture ydl_opts on construction
        original_init = MagicMock(side_effect=lambda opts: captured_opts.append(opts))
        mock_ydl._captured_init = original_init

    return mock_ydl


# ---------------------------------------------------------------------------
# test_download_single_returns_result
# ---------------------------------------------------------------------------


class TestDownloadSingleReturnsResult:
    """download() for a SINGLE URL returns a list with one correct DownloadResult."""

    async def test_download_single_returns_result(self, tmp_path: Path) -> None:
        m4a_file = _create_fake_m4a(tmp_path, VIDEO_ID)
        info = {**FAKE_SINGLE_INFO}

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        captured_opts: list[dict] = []

        def fake_ydl_cls(opts):
            captured_opts.append(opts)
            return _make_ydl_mock(info)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            results = await downloader.download(_make_parsed_single())

        assert len(results) == 1
        result = results[0]
        assert isinstance(result, DownloadResult)
        assert result.video_id == VIDEO_ID
        assert result.title == "Never Gonna Give You Up"
        assert result.artist == "Rick Astley"
        assert result.duration_seconds == 213
        assert (
            result.thumbnail_url
            == "https://i.ytimg.com/vi/dQw4w9WgXcQ/maxresdefault.jpg"
        )
        assert result.file_path == m4a_file
        assert result.file_size_bytes == 1024


# ---------------------------------------------------------------------------
# test_download_single_noplaylist_option_set
# ---------------------------------------------------------------------------


class TestDownloadSingleNoplaylistOption:
    """For SINGLE URLs, yt-dlp opts must include noplaylist=True."""

    async def test_download_single_noplaylist_option_set(self, tmp_path: Path) -> None:
        _create_fake_m4a(tmp_path, VIDEO_ID)

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        captured_opts: list[dict] = []

        def fake_ydl_cls(opts):
            captured_opts.append(opts)
            return _make_ydl_mock(FAKE_SINGLE_INFO)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            await downloader.download(_make_parsed_single())

        assert len(captured_opts) == 1
        assert captured_opts[0]["noplaylist"] is True


# ---------------------------------------------------------------------------
# test_download_radio_mix_as_single
# ---------------------------------------------------------------------------


class TestDownloadRadioMixAsSingle:
    """RADIO_MIX is treated identically to SINGLE (noplaylist=True, one result)."""

    async def test_download_radio_mix_as_single(self, tmp_path: Path) -> None:
        _create_fake_m4a(tmp_path, VIDEO_ID)

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        captured_opts: list[dict] = []

        def fake_ydl_cls(opts):
            captured_opts.append(opts)
            return _make_ydl_mock(FAKE_SINGLE_INFO)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            results = await downloader.download(_make_parsed_radio_mix())

        assert len(results) == 1
        assert results[0].video_id == VIDEO_ID
        assert captured_opts[0]["noplaylist"] is True


# ---------------------------------------------------------------------------
# test_download_playlist_returns_multiple
# ---------------------------------------------------------------------------


class TestDownloadPlaylistReturnsMultiple:
    """PLAYLIST URL returns one DownloadResult per entry."""

    async def test_download_playlist_returns_multiple(self, tmp_path: Path) -> None:
        for entry in FAKE_PLAYLIST_ENTRIES:
            _create_fake_m4a(tmp_path, entry["id"])

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        def fake_ydl_cls(opts):
            return _make_ydl_mock(FAKE_PLAYLIST_INFO)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            results = await downloader.download(_make_parsed_playlist(), max_tracks=10)

        assert len(results) == 3
        ids = {r.video_id for r in results}
        assert ids == {"aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"}


# ---------------------------------------------------------------------------
# test_download_playlist_respects_max_tracks
# ---------------------------------------------------------------------------


class TestDownloadPlaylistRespectsMaxTracks:
    """playlistend in ydl_opts matches the max_tracks argument."""

    async def test_download_playlist_respects_max_tracks(self, tmp_path: Path) -> None:
        for entry in FAKE_PLAYLIST_ENTRIES[:2]:
            _create_fake_m4a(tmp_path, entry["id"])

        # Return only the first 2 entries (simulating yt-dlp honouring playlistend)
        truncated_info = {**FAKE_PLAYLIST_INFO, "entries": FAKE_PLAYLIST_ENTRIES[:2]}

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        captured_opts: list[dict] = []

        def fake_ydl_cls(opts):
            captured_opts.append(opts)
            return _make_ydl_mock(truncated_info)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            results = await downloader.download(_make_parsed_playlist(), max_tracks=2)

        assert captured_opts[0]["playlistend"] == 2
        assert len(results) == 2


# ---------------------------------------------------------------------------
# test_download_progress_callback_called
# ---------------------------------------------------------------------------


class TestDownloadProgressCallbackCalled:
    """Progress callback is invoked at least once during download."""

    async def test_download_progress_callback_called(self, tmp_path: Path) -> None:
        _create_fake_m4a(tmp_path, VIDEO_ID)

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        received: list[DownloadProgress] = []

        async def callback(progress: DownloadProgress) -> None:
            received.append(progress)

        fired_hooks: list = []

        def fake_ydl_cls(opts):
            # Extract and fire the progress hook immediately with fake data
            hooks = opts.get("progress_hooks", [])
            fired_hooks.extend(hooks)
            mock_ydl = _make_ydl_mock(FAKE_SINGLE_INFO)

            def extract_side_effect(url, download=True):  # noqa: FBT002
                for hook in hooks:
                    hook(
                        {
                            "status": "downloading",
                            "downloaded_bytes": 512,
                            "total_bytes": 1024,
                            "speed": 204800.0,
                            "eta": 5,
                            "filename": f"{tmp_path}/{VIDEO_ID}.m4a",
                        }
                    )
                return FAKE_SINGLE_INFO

            mock_ydl.extract_info.side_effect = extract_side_effect
            return mock_ydl

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            await downloader.download(_make_parsed_single(), progress_callback=callback)

        # Drain the event loop: run_coroutine_threadsafe posts from the worker
        # thread; we need at least one iteration after to_thread returns.
        for _ in range(10):
            await asyncio.sleep(0)

        assert len(received) >= 1
        first = received[0]
        assert first.status == "downloading"
        assert first.percentage == pytest.approx(50.0)
        assert first.speed_bps == pytest.approx(204800.0)
        assert first.eta_seconds == 5


# ---------------------------------------------------------------------------
# test_download_file_too_large_raises
# ---------------------------------------------------------------------------


class TestDownloadFileTooLargeRaises:
    """FileTooLargeError raised when downloaded file exceeds max_file_size_bytes."""

    async def test_download_file_too_large_raises(self, tmp_path: Path) -> None:
        max_bytes = 500
        _create_fake_m4a(tmp_path, VIDEO_ID, size_bytes=1000)  # > max_bytes

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=max_bytes
        )

        def fake_ydl_cls(opts):
            return _make_ydl_mock(FAKE_SINGLE_INFO)

        with (
            patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls),
            pytest.raises(FileTooLargeError) as exc_info,
        ):
            await downloader.download(_make_parsed_single())

        err = exc_info.value
        assert err.file_size_bytes == 1000
        assert err.max_bytes == max_bytes


# ---------------------------------------------------------------------------
# test_download_unavailable_video_raises
# ---------------------------------------------------------------------------


class TestDownloadUnavailableVideoRaises:
    """yt-dlp DownloadError is re-raised as VideoUnavailableError."""

    async def test_download_unavailable_video_raises(self, tmp_path: Path) -> None:
        import yt_dlp

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        def fake_ydl_cls(opts):
            mock_ydl = MagicMock()
            mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError(
                "Video unavailable"
            )
            return mock_ydl

        with (
            patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls),
            pytest.raises(VideoUnavailableError),
        ):
            await downloader.download(_make_parsed_single())


# ---------------------------------------------------------------------------
# test_download_result_has_file_path
# ---------------------------------------------------------------------------


class TestDownloadResultHasFilePath:
    """Returned DownloadResult.file_path exists and matches the video_id."""

    async def test_download_result_has_file_path(self, tmp_path: Path) -> None:
        expected_path = _create_fake_m4a(tmp_path, VIDEO_ID)

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        def fake_ydl_cls(opts):
            return _make_ydl_mock(FAKE_SINGLE_INFO)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            results = await downloader.download(_make_parsed_single())

        assert results[0].file_path == expected_path
        assert results[0].file_path.exists()
        assert VIDEO_ID in results[0].file_path.name


# ---------------------------------------------------------------------------
# test_download_cleans_up_on_error
# ---------------------------------------------------------------------------


class TestDownloadCleansUpOnError:
    """Partial files for a video_id are removed when download raises."""

    async def test_download_cleans_up_on_error(self, tmp_path: Path) -> None:
        import yt_dlp

        # Simulate a partial file existing before the error
        partial = tmp_path / f"{VIDEO_ID}.m4a"
        partial.write_bytes(b"partial")

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        def fake_ydl_cls(opts):
            mock_ydl = MagicMock()
            mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError("error")
            return mock_ydl

        with (
            patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls),
            pytest.raises(VideoUnavailableError),
        ):
            await downloader.download(_make_parsed_single())

        assert not partial.exists()


# ---------------------------------------------------------------------------
# test_progress_hook_handles_missing_total_bytes
# ---------------------------------------------------------------------------


class TestProgressHookHandlesMissingTotalBytes:
    """percentage is None when total_bytes is absent from the hook data."""

    async def test_progress_hook_handles_missing_total_bytes(
        self, tmp_path: Path
    ) -> None:
        _create_fake_m4a(tmp_path, VIDEO_ID)

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        received: list[DownloadProgress] = []

        async def callback(progress: DownloadProgress) -> None:
            received.append(progress)

        def fake_ydl_cls(opts):
            hooks = opts.get("progress_hooks", [])
            mock_ydl = _make_ydl_mock(FAKE_SINGLE_INFO)

            def extract_side_effect(url, download=True):  # noqa: FBT002
                for hook in hooks:
                    hook(
                        {
                            "status": "downloading",
                            # No total_bytes, no downloaded_bytes
                            "speed": 100000.0,
                            "eta": None,
                            "filename": str(tmp_path / f"{VIDEO_ID}.m4a"),
                        }
                    )
                return FAKE_SINGLE_INFO

            mock_ydl.extract_info.side_effect = extract_side_effect
            return mock_ydl

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            await downloader.download(_make_parsed_single(), progress_callback=callback)

        for _ in range(10):
            await asyncio.sleep(0)

        assert len(received) >= 1
        assert received[0].percentage is None


# ---------------------------------------------------------------------------
# test_download_result_artist_falls_back_to_channel
# ---------------------------------------------------------------------------


class TestDownloadResultArtistFallback:
    """artist falls back to channel when uploader is absent."""

    async def test_download_result_artist_falls_back_to_channel(
        self, tmp_path: Path
    ) -> None:
        _create_fake_m4a(tmp_path, VIDEO_ID)
        info = {**FAKE_SINGLE_INFO}
        del info["uploader"]  # Remove uploader; channel should be used

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        def fake_ydl_cls(opts):
            return _make_ydl_mock(info)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            results = await downloader.download(_make_parsed_single())

        assert results[0].artist == "RickAstleyVEVO"


# ---------------------------------------------------------------------------
# test_download_result_optional_fields_none
# ---------------------------------------------------------------------------


class TestDownloadResultOptionalFieldsNone:
    """duration_seconds and thumbnail_url can be None when absent from info."""

    async def test_download_result_optional_fields_none(self, tmp_path: Path) -> None:
        _create_fake_m4a(tmp_path, VIDEO_ID)
        info = {
            "id": VIDEO_ID,
            "title": "Minimal Video",
            "ext": "m4a",
            # No uploader, channel, duration, thumbnail
        }

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        def fake_ydl_cls(opts):
            return _make_ydl_mock(info)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            results = await downloader.download(_make_parsed_single())

        r = results[0]
        assert r.artist is None
        assert r.duration_seconds is None
        assert r.thumbnail_url is None


# ---------------------------------------------------------------------------
# test_thumbnail_url_validation
# ---------------------------------------------------------------------------


class TestThumbnailUrlValidation:
    """Only http(s) thumbnail URLs should be accepted; others become None."""

    async def test_https_thumbnail_accepted(self, tmp_path: Path) -> None:
        _create_fake_m4a(tmp_path, VIDEO_ID)
        info = {
            "id": VIDEO_ID,
            "title": "Test",
            "ext": "m4a",
            "thumbnail": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
        }
        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        def fake_ydl_cls(opts):
            return _make_ydl_mock(info)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            results = await downloader.download(_make_parsed_single())

        assert results[0].thumbnail_url == info["thumbnail"]

    async def test_data_uri_thumbnail_rejected(self, tmp_path: Path) -> None:
        _create_fake_m4a(tmp_path, VIDEO_ID)
        info = {
            "id": VIDEO_ID,
            "title": "Test",
            "ext": "m4a",
            "thumbnail": "data:image/jpeg;base64,/9j/4AAQ...",
        }
        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        def fake_ydl_cls(opts):
            return _make_ydl_mock(info)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            results = await downloader.download(_make_parsed_single())

        assert results[0].thumbnail_url is None

    async def test_file_uri_thumbnail_rejected(self, tmp_path: Path) -> None:
        _create_fake_m4a(tmp_path, VIDEO_ID)
        info = {
            "id": VIDEO_ID,
            "title": "Test",
            "ext": "m4a",
            "thumbnail": "file:///etc/passwd",
        }
        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        def fake_ydl_cls(opts):
            return _make_ydl_mock(info)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            results = await downloader.download(_make_parsed_single())

        assert results[0].thumbnail_url is None


# ---------------------------------------------------------------------------
# test_download_playlist_noplaylist_false
# ---------------------------------------------------------------------------


class TestDownloadPlaylistNoplaylistFalse:
    """For PLAYLIST URLs, noplaylist must be False in ydl_opts."""

    async def test_download_playlist_noplaylist_false(self, tmp_path: Path) -> None:
        for entry in FAKE_PLAYLIST_ENTRIES:
            _create_fake_m4a(tmp_path, entry["id"])

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        captured_opts: list[dict] = []

        def fake_ydl_cls(opts):
            captured_opts.append(opts)
            return _make_ydl_mock(FAKE_PLAYLIST_INFO)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            await downloader.download(_make_parsed_playlist(), max_tracks=10)

        assert captured_opts[0]["noplaylist"] is False


# ---------------------------------------------------------------------------
# test_find_audio_file_fallback_extension
# ---------------------------------------------------------------------------


class TestFindAudioFileFallbackExtension:
    """_find_audio_file falls back to non-m4a extension when .m4a is absent."""

    async def test_find_audio_file_fallback_known_extension(
        self, tmp_path: Path
    ) -> None:
        # .webm is in the known-extension loop — exercises that branch
        webm_file = tmp_path / f"{VIDEO_ID}.webm"
        webm_file.write_bytes(b"x" * 512)

        info = {**FAKE_SINGLE_INFO}

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        def fake_ydl_cls(opts):
            return _make_ydl_mock(info)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            results = await downloader.download(_make_parsed_single())

        assert results[0].file_path == webm_file

    async def test_find_audio_file_fallback_extension(self, tmp_path: Path) -> None:
        # Create a .flac file (not in known-extension list) — exercises glob fallback
        flac_file = tmp_path / f"{VIDEO_ID}.flac"
        flac_file.write_bytes(b"x" * 512)

        info = {**FAKE_SINGLE_INFO}

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        def fake_ydl_cls(opts):
            return _make_ydl_mock(info)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            results = await downloader.download(_make_parsed_single())

        assert results[0].file_path == flac_file


# ---------------------------------------------------------------------------
# test_find_audio_file_not_found_raises
# ---------------------------------------------------------------------------


class TestFindAudioFileNotFoundRaises:
    """DownloadError is raised when no audio file exists for video_id."""

    async def test_find_audio_file_not_found_raises(self, tmp_path: Path) -> None:
        # Do NOT create any file — forces the glob fallback and then the raise
        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        def fake_ydl_cls(opts):
            return _make_ydl_mock(FAKE_SINGLE_INFO)

        with (
            patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls),
            pytest.raises(DownloadError, match="Downloaded file not found"),
        ):
            await downloader.download(_make_parsed_single())


# ---------------------------------------------------------------------------
# test_progress_hook_no_callback_is_noop
# ---------------------------------------------------------------------------


class TestProgressHookNoCallbackIsNoop:
    """Progress hook with callback=None does not raise."""

    async def test_progress_hook_no_callback_is_noop(self, tmp_path: Path) -> None:
        _create_fake_m4a(tmp_path, VIDEO_ID)

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        def fake_ydl_cls(opts):
            hooks = opts.get("progress_hooks", [])
            mock_ydl = _make_ydl_mock(FAKE_SINGLE_INFO)

            def extract_side_effect(url, download=True):  # noqa: FBT002
                for hook in hooks:
                    hook({"status": "downloading", "downloaded_bytes": 100})
                return FAKE_SINGLE_INFO

            mock_ydl.extract_info.side_effect = extract_side_effect
            return mock_ydl

        # No progress_callback — must not raise
        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            results = await downloader.download(
                _make_parsed_single(), progress_callback=None
            )

        assert len(results) == 1


# ---------------------------------------------------------------------------
# test_build_result_rejects_invalid_video_id
# ---------------------------------------------------------------------------


class TestBuildResultRejectsInvalidVideoId:
    """_build_result raises DownloadError when yt-dlp returns an unexpected video id."""

    async def test_build_result_rejects_invalid_video_id(self, tmp_path: Path) -> None:
        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        def fake_ydl_cls(opts):
            # info_dict with a path-traversal-style id
            evil_info = {**FAKE_SINGLE_INFO, "id": "../evil"}
            return _make_ydl_mock(evil_info)

        with (
            patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls),
            pytest.raises(DownloadError, match="unexpected video id"),
        ):
            await downloader.download(_make_parsed_single())

    async def test_build_result_rejects_empty_video_id(self, tmp_path: Path) -> None:
        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        def fake_ydl_cls(opts):
            empty_info = {**FAKE_SINGLE_INFO, "id": ""}
            return _make_ydl_mock(empty_info)

        with (
            patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls),
            pytest.raises(DownloadError, match="unexpected video id"),
        ):
            await downloader.download(_make_parsed_single())


# ---------------------------------------------------------------------------
# test_cleanup_partials_none_video_id
# ---------------------------------------------------------------------------


class TestCleanupPartialsNoneVideoId:
    """_cleanup_partials is a no-op when video_id is None."""

    def test_cleanup_partials_none_video_id(self, tmp_path: Path) -> None:
        """Calling _cleanup_partials(None) must return without touching filesystem."""
        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )
        sentinel = tmp_path / "should_remain.txt"
        sentinel.write_text("keep me")

        downloader._cleanup_partials(None)

        assert sentinel.exists()  # nothing was deleted


# ---------------------------------------------------------------------------
# test_cleanup_partials_oserror_is_swallowed
# ---------------------------------------------------------------------------


class TestPostprocessorOrder:
    """Postprocessors must be in explicit order: convertor → embed → metadata."""

    async def test_postprocessor_order_thumbnail_before_embed_before_metadata(
        self, tmp_path: Path
    ) -> None:
        _create_fake_m4a(tmp_path, VIDEO_ID)

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        captured_opts: list[dict] = []

        def fake_ydl_cls(opts):
            captured_opts.append(opts)
            return _make_ydl_mock(FAKE_SINGLE_INFO)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            await downloader.download(_make_parsed_single())

        opts = captured_opts[0]
        pps = opts["postprocessors"]
        # Must have exactly 3 explicit postprocessors
        assert len(pps) == 3
        assert pps[0]["key"] == "FFmpegThumbnailsConvertor"
        assert pps[1]["key"] == "FFmpegMetadata"
        assert pps[2]["key"] == "EmbedThumbnail"
        # embedthumbnail and addmetadata flags must be removed
        assert "embedthumbnail" not in opts
        assert "addmetadata" not in opts
        # writethumbnail must still be present
        assert opts.get("writethumbnail") is True


class TestCleanupPartialsOSErrorSwallowed:
    """OSError during partial file removal is swallowed without propagating."""

    async def test_cleanup_partials_oserror_is_swallowed(self, tmp_path: Path) -> None:
        import yt_dlp

        partial = tmp_path / f"{VIDEO_ID}.m4a"
        partial.write_bytes(b"partial")

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        def fake_ydl_cls(opts):
            mock_ydl = MagicMock()
            mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError("err")
            return mock_ydl

        # Patch Path.unlink to raise OSError — cleanup must not propagate it
        def unlink_raises(self, missing_ok=False):  # noqa: FBT002
            raise OSError("Permission denied")

        with (
            patch.object(Path, "unlink", unlink_raises),
            patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls),
            pytest.raises(VideoUnavailableError),
        ):
            await downloader.download(_make_parsed_single())


# ---------------------------------------------------------------------------
# Chapter extraction
# ---------------------------------------------------------------------------


class TestChapterExtraction:
    async def test_chapters_extracted_from_info(self, tmp_path: Path) -> None:
        """yt-dlp info with chapters populates DownloadResult.chapters."""
        info = {
            **FAKE_SINGLE_INFO,
            "chapters": [
                {"start_time": 0.0, "end_time": 60.0, "title": "Intro"},
                {"start_time": 60.0, "end_time": 180.0, "title": "Verse 1"},
                {"start_time": 180.0, "end_time": 213.0, "title": "Chorus"},
            ],
        }
        _create_fake_m4a(tmp_path, VIDEO_ID)
        downloader = AudioDownloader(tmp_path, max_file_size_bytes=10**9)
        ydl_mock = _make_ydl_mock(info)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", return_value=ydl_mock):
            results = await downloader.download(_make_parsed_single())

        assert results[0].chapters == ((0, "Intro"), (60, "Verse 1"), (180, "Chorus"))

    async def test_chapters_none_when_absent(self, tmp_path: Path) -> None:
        """Info without chapters key produces chapters=None."""
        info = {**FAKE_SINGLE_INFO}
        info.pop("chapters", None)
        _create_fake_m4a(tmp_path, VIDEO_ID)
        downloader = AudioDownloader(tmp_path, max_file_size_bytes=10**9)
        ydl_mock = _make_ydl_mock(info)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", return_value=ydl_mock):
            results = await downloader.download(_make_parsed_single())

        assert results[0].chapters is None

    async def test_chapters_none_when_empty_list(self, tmp_path: Path) -> None:
        """Info with chapters=[] produces chapters=None."""
        info = {**FAKE_SINGLE_INFO, "chapters": []}
        _create_fake_m4a(tmp_path, VIDEO_ID)
        downloader = AudioDownloader(tmp_path, max_file_size_bytes=10**9)
        ydl_mock = _make_ydl_mock(info)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", return_value=ydl_mock):
            results = await downloader.download(_make_parsed_single())

        assert results[0].chapters is None


# ---------------------------------------------------------------------------
# test_download_timeout
# ---------------------------------------------------------------------------


class TestDownloadTimeout:
    """Download timeout wraps asyncio.wait_for and raises DownloadError."""

    async def test_single_download_timeout_raises(self, tmp_path: Path) -> None:
        """A single download that exceeds timeout raises DownloadError."""
        import time

        def slow_extract(*_args, **_kwargs):
            time.sleep(5)
            return FAKE_SINGLE_INFO

        mock_ydl = MagicMock()
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl.extract_info.side_effect = slow_extract

        downloader = AudioDownloader(
            tmp_path, max_file_size_bytes=10**9, download_timeout=1
        )

        with (
            patch("src.downloader.client.yt_dlp.YoutubeDL", return_value=mock_ydl),
            pytest.raises(DownloadError, match="timed out"),
        ):
            await downloader.download(_make_parsed_single())

    async def test_playlist_download_timeout_raises(self, tmp_path: Path) -> None:
        """A playlist download that exceeds timeout raises DownloadError."""
        import time

        def slow_extract(*_args, **_kwargs):
            time.sleep(5)
            return {"entries": []}

        mock_ydl = MagicMock()
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl.extract_info.side_effect = slow_extract

        downloader = AudioDownloader(
            tmp_path, max_file_size_bytes=10**9, download_timeout=1
        )

        with (
            patch("src.downloader.client.yt_dlp.YoutubeDL", return_value=mock_ydl),
            pytest.raises(DownloadError, match="timed out"),
        ):
            await downloader.download(_make_parsed_playlist())

    async def test_socket_timeout_set_in_ydl_opts(self, tmp_path: Path) -> None:
        """socket_timeout is passed to yt-dlp options."""
        _create_fake_m4a(tmp_path, VIDEO_ID)
        downloader = AudioDownloader(
            tmp_path, max_file_size_bytes=10**9, download_timeout=600
        )
        mock_ydl = _make_ydl_mock(FAKE_SINGLE_INFO)

        with patch(
            "src.downloader.client.yt_dlp.YoutubeDL",
            side_effect=lambda opts: mock_ydl,
        ) as mock_cls:
            await downloader.download(_make_parsed_single())
            call_opts = mock_cls.call_args[0][0]
            assert call_opts["socket_timeout"] == 600


# ---------------------------------------------------------------------------
# Per-media inflight dedup
# ---------------------------------------------------------------------------


class TestInflightDedup:
    async def test_concurrent_same_id_serialized(self, tmp_path: Path) -> None:
        """Two concurrent downloads of the same video_id must not overlap."""
        call_order: list[str] = []
        downloader = AudioDownloader(tmp_path, max_file_size_bytes=10**9)

        _create_fake_m4a(tmp_path, VIDEO_ID)
        mock_ydl = _make_ydl_mock(FAKE_SINGLE_INFO)

        async def fake_run_blocking(func, /, *args, **kwargs):
            """Yield once so a second task can contend for the same media lock."""
            call_order.append("enter")
            await asyncio.sleep(0)
            result = func(*args, **kwargs)
            call_order.append("exit")
            return result

        with (
            patch("src.downloader.client.yt_dlp.YoutubeDL", return_value=mock_ydl),
            patch("src.downloader.client._run_blocking", new=fake_run_blocking),
        ):
            tasks = [
                asyncio.create_task(downloader.download(_make_parsed_single())),
                asyncio.create_task(downloader.download(_make_parsed_single())),
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

        # With per-media lock, entries should be serialized: enter/exit/enter/exit
        # Without it, we'd see enter/enter/exit/exit
        assert call_order == ["enter", "exit", "enter", "exit"]

    async def test_inflight_lock_cleaned_up(self, tmp_path: Path) -> None:
        """Inflight lock entry is removed after download completes."""
        downloader = AudioDownloader(tmp_path, max_file_size_bytes=10**9)
        _create_fake_m4a(tmp_path, VIDEO_ID)
        mock_ydl = _make_ydl_mock(FAKE_SINGLE_INFO)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", return_value=mock_ydl):
            await downloader.download(_make_parsed_single())

        assert VIDEO_ID not in downloader._inflight

    async def test_three_concurrent_all_serialized(self, tmp_path: Path) -> None:
        """Three concurrent downloads must all be serialized — no overlap."""
        call_order: list[str] = []
        downloader = AudioDownloader(tmp_path, max_file_size_bytes=10**9)

        _create_fake_m4a(tmp_path, VIDEO_ID)
        mock_ydl = _make_ydl_mock(FAKE_SINGLE_INFO)

        async def fake_run_blocking(func, /, *args, **kwargs):
            call_order.append("enter")
            await asyncio.sleep(0)
            result = func(*args, **kwargs)
            call_order.append("exit")
            return result

        with (
            patch("src.downloader.client.yt_dlp.YoutubeDL", return_value=mock_ydl),
            patch("src.downloader.client._run_blocking", new=fake_run_blocking),
        ):
            tasks = [
                asyncio.create_task(downloader.download(_make_parsed_single())),
                asyncio.create_task(downloader.download(_make_parsed_single())),
                asyncio.create_task(downloader.download(_make_parsed_single())),
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

        # All three must be fully serialized
        assert call_order == ["enter", "exit", "enter", "exit", "enter", "exit"]


# ===========================================================================
# Playlist sparse / None entries
# ===========================================================================


class TestPlaylistSparseEntries:
    """Playlist downloads should skip None and incomplete entries."""

    async def test_none_entry_skipped(self, tmp_path: Path) -> None:
        """None entry in playlist should be skipped, not crash."""
        good_entry = FAKE_PLAYLIST_ENTRIES[0]
        _create_fake_m4a(tmp_path, good_entry["id"])

        info_with_none = {
            **FAKE_PLAYLIST_INFO,
            "entries": [None, good_entry, None],
        }
        mock_ydl = _make_ydl_mock(info_with_none)

        downloader = AudioDownloader(download_dir=tmp_path, max_file_size_bytes=10**9)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", return_value=mock_ydl):
            results = await downloader.download(_make_parsed_playlist())

        assert len(results) == 1
        assert results[0].video_id == good_entry["id"]

    async def test_entry_without_id_skipped(self, tmp_path: Path) -> None:
        """Entry missing 'id' key should be skipped."""
        good_entry = FAKE_PLAYLIST_ENTRIES[0]
        _create_fake_m4a(tmp_path, good_entry["id"])
        bad_entry = {"title": "No ID track"}

        info = {
            **FAKE_PLAYLIST_INFO,
            "entries": [bad_entry, good_entry],
        }
        mock_ydl = _make_ydl_mock(info)

        downloader = AudioDownloader(download_dir=tmp_path, max_file_size_bytes=10**9)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", return_value=mock_ydl):
            results = await downloader.download(_make_parsed_playlist())

        assert len(results) == 1
