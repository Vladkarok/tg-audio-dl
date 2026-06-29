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
from src.downloader.url_parser import ParsedURL, Platform, URLType

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

# Flat metadata entries (returned by extract_flat — no audio info, just id+title)
FAKE_FLAT_ENTRIES: list[dict] = [
    {"id": "aaaaaaaaaaa", "title": "Track 1", "_type": "url"},
    {"id": "bbbbbbbbbbb", "title": "Track 2", "_type": "url"},
    {"id": "ccccccccccc", "title": "Track 3", "_type": "url"},
]

FAKE_FLAT_PLAYLIST_INFO: dict = {
    "id": PLAYLIST_ID,
    "title": "My Playlist",
    "_type": "playlist",
    "entries": FAKE_FLAT_ENTRIES,
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
    """PLAYLIST URL returns one DownloadResult per entry via per-track downloads."""

    async def test_download_playlist_returns_multiple(self, tmp_path: Path) -> None:
        for entry in FAKE_PLAYLIST_ENTRIES:
            _create_fake_m4a(tmp_path, entry["id"])

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        # Flat call returns metadata; per-track calls return individual entry info
        flat_mock = _make_ydl_mock(FAKE_FLAT_PLAYLIST_INFO)
        per_track_mocks = [_make_ydl_mock(e) for e in FAKE_PLAYLIST_ENTRIES]

        with patch(
            "src.downloader.client.yt_dlp.YoutubeDL",
            side_effect=[flat_mock, *per_track_mocks],
        ):
            results = await downloader.download(_make_parsed_playlist(), max_tracks=10)

        assert len(results) == 3
        ids = {r.video_id for r in results}
        assert ids == {"aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"}


class TestDownloadSoundCloudPlaylistUsesTrackUrls:
    """SoundCloud set tracks download by URL, not a fabricated youtube.com URL."""

    async def test_soundcloud_set_downloads_by_url(self, tmp_path: Path) -> None:
        sc_flat = {
            "id": "myset",
            "_type": "playlist",
            "entries": [
                {
                    "id": "100000001",  # numeric — would match _TRACK_ID_RE
                    "title": "T1",
                    "url": "https://soundcloud.com/artist/t1",
                    "_type": "url",
                },
                {
                    "id": "100000002",
                    "title": "T2",
                    "url": "https://soundcloud.com/artist/t2",
                    "_type": "url",
                },
            ],
        }
        track_infos = [
            {"id": "100000001", "title": "T1", "uploader": "artist", "ext": "m4a"},
            {"id": "100000002", "title": "T2", "uploader": "artist", "ext": "m4a"},
        ]
        for ti in track_infos:
            _create_fake_m4a(tmp_path, ti["id"])

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        captured_urls: list[str] = []
        track_iter = iter(track_infos)

        def fake_ydl_cls(opts):
            if opts.get("extract_flat"):
                return _make_ydl_mock(sc_flat)
            m = MagicMock()
            m.__enter__ = MagicMock(return_value=m)
            m.__exit__ = MagicMock(return_value=False)
            info = next(track_iter)

            def extract(url, download):
                captured_urls.append(url)
                return info

            m.extract_info.side_effect = extract
            return m

        parsed = ParsedURL(
            url_type=URLType.PLAYLIST,
            video_id=None,
            playlist_id="artist/sets/myset",
            canonical_url="https://soundcloud.com/artist/sets/myset",
            original_url="https://soundcloud.com/artist/sets/myset",
            platform=Platform.SOUNDCLOUD,
        )

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            results = await downloader.download(parsed, max_tracks=10)

        # Per-track downloads use the SoundCloud URLs, never youtube.com/watch.
        assert captured_urls == [
            "https://soundcloud.com/artist/t1",
            "https://soundcloud.com/artist/t2",
        ]
        # Cache keys are the derived sc_ slugs, not the numeric ids.
        assert all(r.video_id.startswith("sc_") for r in results)


class TestOversizedFileCleanedUp:
    """An oversized rejected download is removed from disk, not left to linger."""

    async def test_oversized_file_deleted(self, tmp_path: Path) -> None:
        f = _create_fake_m4a(tmp_path, VIDEO_ID, size_bytes=1000)
        downloader = AudioDownloader(download_dir=tmp_path, max_file_size_bytes=500)

        with (
            patch(
                "src.downloader.client.yt_dlp.YoutubeDL",
                side_effect=lambda opts: _make_ydl_mock(FAKE_SINGLE_INFO),
            ),
            pytest.raises(FileTooLargeError),
        ):
            await downloader.download(_make_parsed_single())

        assert not f.exists()


class TestFindAudioFileIgnoresSidecars:
    """A leftover sidecar must never be returned as the audio file."""

    async def test_only_thumbnail_sidecar_raises(self, tmp_path: Path) -> None:
        # yt-dlp "returns" info but only a .jpg thumbnail remains on disk.
        (tmp_path / f"{VIDEO_ID}.jpg").write_bytes(b"img")
        (tmp_path / f"{VIDEO_ID}.info.json").write_bytes(b"{}")
        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        with (
            patch(
                "src.downloader.client.yt_dlp.YoutubeDL",
                side_effect=lambda opts: _make_ydl_mock(FAKE_SINGLE_INFO),
            ),
            pytest.raises(DownloadError),
        ):
            await downloader.download(_make_parsed_single())

    async def test_unusual_audio_container_accepted(self, tmp_path: Path) -> None:
        """An audio container not in the preferred list (e.g. .weba) is still used."""
        # Only a .weba file plus a thumbnail sidecar are present.
        audio = tmp_path / f"{VIDEO_ID}.weba"
        audio.write_bytes(b"x" * 1024)
        (tmp_path / f"{VIDEO_ID}.jpg").write_bytes(b"img")
        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        with patch(
            "src.downloader.client.yt_dlp.YoutubeDL",
            side_effect=lambda opts: _make_ydl_mock(FAKE_SINGLE_INFO),
        ):
            results = await downloader.download(_make_parsed_single())

        assert results[0].file_path == audio


# ---------------------------------------------------------------------------
# test_download_playlist_respects_max_tracks
# ---------------------------------------------------------------------------


class TestDownloadPlaylistRespectsMaxTracks:
    """max_tracks is passed to the flat metadata fetch and limits results."""

    async def test_download_playlist_respects_max_tracks(self, tmp_path: Path) -> None:
        for entry in FAKE_PLAYLIST_ENTRIES[:2]:
            _create_fake_m4a(tmp_path, entry["id"])

        truncated_flat = {**FAKE_FLAT_PLAYLIST_INFO, "entries": FAKE_FLAT_ENTRIES[:2]}
        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        captured_opts: list[dict] = []

        def fake_ydl_cls(opts):
            captured_opts.append(opts)
            # First call is flat metadata; subsequent are per-track
            if opts.get("extract_flat"):
                return _make_ydl_mock(truncated_flat)
            # Determine which track based on call count (skip flat call)
            idx = sum(1 for o in captured_opts if not o.get("extract_flat")) - 1
            return _make_ydl_mock(FAKE_PLAYLIST_ENTRIES[idx])

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            results = await downloader.download(_make_parsed_playlist(), max_tracks=2)

        # Flat fetch must have playlistend=2
        flat_opts = next(o for o in captured_opts if o.get("extract_flat"))
        assert flat_opts["playlistend"] == 2
        assert len(results) == 2


# ---------------------------------------------------------------------------
# test_track_start_callback
# ---------------------------------------------------------------------------


class TestTrackStartCallback:
    """track_start_callback fires with (index, total, title) before each track."""

    async def test_track_start_callback_called_per_track(self, tmp_path: Path) -> None:
        for entry in FAKE_PLAYLIST_ENTRIES:
            _create_fake_m4a(tmp_path, entry["id"])

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        flat_mock = _make_ydl_mock(FAKE_FLAT_PLAYLIST_INFO)
        per_track_mocks = [_make_ydl_mock(e) for e in FAKE_PLAYLIST_ENTRIES]

        received: list[tuple[int, int, str]] = []

        async def on_track_start(idx: int, total: int, title: str) -> None:
            received.append((idx, total, title))

        with patch(
            "src.downloader.client.yt_dlp.YoutubeDL",
            side_effect=[flat_mock, *per_track_mocks],
        ):
            await downloader.download(
                _make_parsed_playlist(),
                max_tracks=10,
                track_start_callback=on_track_start,
            )

        assert len(received) == 3
        assert received[0] == (1, 3, "Track 1")
        assert received[1] == (2, 3, "Track 2")
        assert received[2] == (3, 3, "Track 3")


# ---------------------------------------------------------------------------
# test_track_ready_callback
# ---------------------------------------------------------------------------


class TestTrackReadyCallback:
    """track_ready_callback fires immediately after each track is downloaded."""

    async def test_track_ready_callback_fires_per_track(self, tmp_path: Path) -> None:
        for entry in FAKE_PLAYLIST_ENTRIES:
            _create_fake_m4a(tmp_path, entry["id"])

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        flat_mock = _make_ydl_mock(FAKE_FLAT_PLAYLIST_INFO)
        per_track_mocks = [_make_ydl_mock(e) for e in FAKE_PLAYLIST_ENTRIES]

        ready_results: list[DownloadResult] = []

        async def on_track_ready(result: DownloadResult) -> None:
            ready_results.append(result)

        with patch(
            "src.downloader.client.yt_dlp.YoutubeDL",
            side_effect=[flat_mock, *per_track_mocks],
        ):
            results = await downloader.download(
                _make_parsed_playlist(),
                max_tracks=10,
                track_ready_callback=on_track_ready,
            )

        # Callback fired once per track
        assert len(ready_results) == 3
        # Same results returned from download()
        assert {r.video_id for r in ready_results} == {r.video_id for r in results}


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


class TestDownloadErrorClassification:
    """Download-path errors use the same marker classification as metadata.

    Transient failures (proxy/network/rate-limit) must surface as generic
    DownloadError so the user is not told the video is unavailable.
    """

    @staticmethod
    def _fake_ydl_cls_raising(message: str):
        import yt_dlp

        def fake_ydl_cls(opts):
            mock_ydl = MagicMock()
            mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError(message)
            return mock_ydl

        return fake_ydl_cls

    async def test_transient_error_is_generic_download_error(
        self, tmp_path: Path
    ) -> None:
        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )
        fake_cls = self._fake_ydl_cls_raising(
            "Unable to download webpage: HTTP Error 503: Service Unavailable"
        )
        with (
            patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_cls),
            pytest.raises(DownloadError) as excinfo,
        ):
            await downloader.download(_make_parsed_single())
        assert not isinstance(excinfo.value, VideoUnavailableError)

    async def test_geo_block_is_video_unavailable(self, tmp_path: Path) -> None:
        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )
        fake_cls = self._fake_ydl_cls_raising(
            "The uploader has not made this video available in your country"
        )
        with (
            patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_cls),
            pytest.raises(VideoUnavailableError),
        ):
            await downloader.download(_make_parsed_single())

    async def test_transient_error_still_cleans_partials(self, tmp_path: Path) -> None:
        partial = tmp_path / f"{VIDEO_ID}.m4a"
        partial.write_bytes(b"partial")
        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )
        fake_cls = self._fake_ydl_cls_raising("Connection reset by peer")
        with (
            patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_cls),
            pytest.raises(DownloadError),
        ):
            await downloader.download(_make_parsed_single())
        assert not partial.exists()


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
            pytest.raises(DownloadError),
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
    """Flat metadata call uses noplaylist=False; per-track calls use noplaylist=True."""

    async def test_download_playlist_noplaylist_false(self, tmp_path: Path) -> None:
        for entry in FAKE_PLAYLIST_ENTRIES:
            _create_fake_m4a(tmp_path, entry["id"])

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        captured_opts: list[dict] = []

        def fake_ydl_cls(opts):
            captured_opts.append(opts)
            if opts.get("extract_flat"):
                return _make_ydl_mock(FAKE_FLAT_PLAYLIST_INFO)
            idx = sum(1 for o in captured_opts if not o.get("extract_flat")) - 1
            return _make_ydl_mock(FAKE_PLAYLIST_ENTRIES[idx])

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            await downloader.download(_make_parsed_playlist(), max_tracks=10)

        flat_opts = next(o for o in captured_opts if o.get("extract_flat"))
        assert flat_opts["noplaylist"] is False


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
            pytest.raises(DownloadError),
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

    async def test_chapters_skip_untitled_placeholders(self, tmp_path: Path) -> None:
        """yt-dlp <Untitled Chapter N> placeholders are filtered out."""
        info = {
            **FAKE_SINGLE_INFO,
            "chapters": [
                {"start_time": 0.0, "end_time": 15.0, "title": "<Untitled Chapter 1>"},
                {"start_time": 15.0, "end_time": 369.0, "title": "Rank 1 - Awakening"},
                {
                    "start_time": 369.0,
                    "end_time": 635.0,
                    "title": "Oceanlab - Sky Falls Down",
                },
            ],
        }
        _create_fake_m4a(tmp_path, VIDEO_ID)
        downloader = AudioDownloader(tmp_path, max_file_size_bytes=10**9)
        ydl_mock = _make_ydl_mock(info)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", return_value=ydl_mock):
            results = await downloader.download(_make_parsed_single())

        assert results[0].chapters == (
            (15, "Rank 1 - Awakening"),
            (369, "Oceanlab - Sky Falls Down"),
        )


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
        """A playlist flat metadata fetch that exceeds timeout raises DownloadError."""
        import time

        def slow_extract(*_args, **_kwargs):
            time.sleep(5)
            return {"entries": []}

        mock_ydl = MagicMock()
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl.extract_info.side_effect = slow_extract

        # download_timeout=1 now governs the flat metadata fetch too
        downloader = AudioDownloader(
            tmp_path, max_file_size_bytes=10**9, download_timeout=1
        )

        with (
            patch("src.downloader.client.yt_dlp.YoutubeDL", return_value=mock_ydl),
            pytest.raises(DownloadError, match="[Tt]imed out"),
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
# Per-download isolation (H2)
# ===========================================================================


def _writing_ydl_cls(info: dict, captured_dirs: list[Path]):
    """YoutubeDL factory whose extract_info writes the audio into outtmpl's dir.

    Mimics real yt-dlp: the audio lands in the per-download isolation dir
    derived from ``opts['outtmpl']`` rather than the flat download_dir. Records
    each download's directory so tests can assert isolation.
    """

    def factory(opts):
        out_dir = Path(opts["outtmpl"]).parent
        captured_dirs.append(out_dir)
        mock_ydl = _make_ydl_mock(info)

        def extract(url, download=True):  # noqa: FBT002
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{info['id']}.m4a").write_bytes(b"x" * 1024)
            return info

        mock_ydl.extract_info.side_effect = extract
        return mock_ydl

    return factory


class TestDownloadIsolation:
    """Each download writes into its own ``.dl-<token>`` dir (H2)."""

    async def test_download_writes_to_isolated_dir(self, tmp_path: Path) -> None:
        """yt-dlp's outtmpl points at a private ``.dl-*`` subdir, not the flat
        download_dir, and the kept file is moved to the canonical location.
        """
        captured_dirs: list[Path] = []
        downloader = AudioDownloader(download_dir=tmp_path, max_file_size_bytes=10**9)

        with patch(
            "src.downloader.client.yt_dlp.YoutubeDL",
            side_effect=_writing_ydl_cls(FAKE_SINGLE_INFO, captured_dirs),
        ):
            results = await downloader.download(_make_parsed_single())

        # The download wrote into a per-download isolation dir under tmp_path.
        assert len(captured_dirs) == 1
        dl_dir = captured_dirs[0]
        assert dl_dir.parent == tmp_path
        assert dl_dir.name.startswith(".dl-")
        # Final file is at the canonical flat location, isolation dir is gone.
        assert results[0].file_path == tmp_path / f"{VIDEO_ID}.m4a"
        assert results[0].file_path.exists()
        assert not dl_dir.exists()

    async def test_overlapping_same_id_use_separate_dirs(self, tmp_path: Path) -> None:
        """Two downloads of the SAME id never share an on-disk location.

        This is the core H2 guarantee: a lingering timed-out worker thread for
        video X writes into its own ``.dl-token1`` dir and can never clobber a
        fresh request for X which uses ``.dl-token2``.
        """
        captured_dirs: list[Path] = []
        downloader = AudioDownloader(download_dir=tmp_path, max_file_size_bytes=10**9)

        with patch(
            "src.downloader.client.yt_dlp.YoutubeDL",
            side_effect=_writing_ydl_cls(FAKE_SINGLE_INFO, captured_dirs),
        ):
            r1 = await downloader.download(_make_parsed_single())
            r2 = await downloader.download(_make_parsed_single())

        assert len(captured_dirs) == 2
        # Distinct isolation dirs — no collision possible.
        assert captured_dirs[0] != captured_dirs[1]
        assert all(d.name.startswith(".dl-") for d in captured_dirs)
        # Both produce the same canonical result and leave no isolation dirs.
        assert r1[0].file_path == r2[0].file_path == tmp_path / f"{VIDEO_ID}.m4a"
        assert not any(p.name.startswith(".dl-") for p in tmp_path.iterdir())

    async def test_isolation_dir_removed_on_error(self, tmp_path: Path) -> None:
        """A failed download leaves no ``.dl-*`` dir behind."""
        import yt_dlp

        captured_dirs: list[Path] = []
        downloader = AudioDownloader(download_dir=tmp_path, max_file_size_bytes=10**9)

        def factory(opts):
            captured_dirs.append(Path(opts["outtmpl"]).parent)
            mock_ydl = MagicMock()
            mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl.__exit__ = MagicMock(return_value=False)
            mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError("boom")
            return mock_ydl

        with (
            patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=factory),
            pytest.raises(DownloadError),
        ):
            await downloader.download(_make_parsed_single())

        assert captured_dirs and not captured_dirs[0].exists()
        assert not any(p.name.startswith(".dl-") for p in tmp_path.iterdir())

    async def test_oversized_in_isolated_dir_cleaned(self, tmp_path: Path) -> None:
        """An oversized file written into the isolation dir is fully removed."""
        captured_dirs: list[Path] = []
        downloader = AudioDownloader(download_dir=tmp_path, max_file_size_bytes=500)

        def factory(opts):
            out_dir = Path(opts["outtmpl"]).parent
            captured_dirs.append(out_dir)
            mock_ydl = _make_ydl_mock(FAKE_SINGLE_INFO)

            def extract(url, download=True):  # noqa: FBT002
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / f"{VIDEO_ID}.m4a").write_bytes(b"x" * 1000)  # > 500
                return FAKE_SINGLE_INFO

            mock_ydl.extract_info.side_effect = extract
            return mock_ydl

        with (
            patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=factory),
            pytest.raises(FileTooLargeError),
        ):
            await downloader.download(_make_parsed_single())

        # Neither the isolation dir nor a moved canonical file survives.
        assert not captured_dirs[0].exists()
        assert not (tmp_path / f"{VIDEO_ID}.m4a").exists()
        assert not any(p.name.startswith(".dl-") for p in tmp_path.iterdir())


# ===========================================================================
# Playlist sparse / None entries
# ===========================================================================


class TestPlaylistSparseEntries:
    """Playlist downloads should skip None and incomplete entries."""

    async def test_none_entry_skipped(self, tmp_path: Path) -> None:
        """None entry in flat metadata should be skipped; valid entry downloads."""
        good_entry = FAKE_PLAYLIST_ENTRIES[0]
        _create_fake_m4a(tmp_path, good_entry["id"])

        # Flat metadata: None entries filtered, one valid entry kept
        flat_info = {
            **FAKE_FLAT_PLAYLIST_INFO,
            "entries": [
                None,
                {"id": good_entry["id"], "title": good_entry["title"]},
                None,
            ],
        }
        flat_mock = _make_ydl_mock(flat_info)
        track_mock = _make_ydl_mock(good_entry)

        downloader = AudioDownloader(download_dir=tmp_path, max_file_size_bytes=10**9)

        with patch(
            "src.downloader.client.yt_dlp.YoutubeDL",
            side_effect=[flat_mock, track_mock],
        ):
            results = await downloader.download(_make_parsed_playlist())

        assert len(results) == 1
        assert results[0].video_id == good_entry["id"]

    async def test_entry_without_id_skipped(self, tmp_path: Path) -> None:
        """Entry missing 'id' key in flat metadata should be skipped."""
        good_entry = FAKE_PLAYLIST_ENTRIES[0]
        _create_fake_m4a(tmp_path, good_entry["id"])

        flat_info = {
            **FAKE_FLAT_PLAYLIST_INFO,
            "entries": [
                {"title": "No ID track"},
                {"id": good_entry["id"], "title": good_entry["title"]},
            ],
        }
        flat_mock = _make_ydl_mock(flat_info)
        track_mock = _make_ydl_mock(good_entry)

        downloader = AudioDownloader(download_dir=tmp_path, max_file_size_bytes=10**9)

        with patch(
            "src.downloader.client.yt_dlp.YoutubeDL",
            side_effect=[flat_mock, track_mock],
        ):
            results = await downloader.download(_make_parsed_playlist())

        assert len(results) == 1

    async def test_all_entries_none_raises(self, tmp_path: Path) -> None:
        """Playlist where flat metadata returns only None entries raises DownloadError."""
        flat_info = {**FAKE_FLAT_PLAYLIST_INFO, "entries": [None, None]}
        flat_mock = _make_ydl_mock(flat_info)

        downloader = AudioDownloader(download_dir=tmp_path, max_file_size_bytes=10**9)

        with (
            patch("src.downloader.client.yt_dlp.YoutubeDL", return_value=flat_mock),
            pytest.raises(DownloadError, match="empty or unavailable"),
        ):
            await downloader.download(_make_parsed_playlist())


# ---------------------------------------------------------------------------
# fetch_metadata — metadata-only refresh, no audio download
# ---------------------------------------------------------------------------


class TestFetchMetadata:
    """AudioDownloader.fetch_metadata returns TrackMetadata without downloading audio."""

    async def test_fetch_metadata_returns_chapters(self, tmp_path: Path) -> None:
        from src.downloader.client import TrackMetadata

        info = {
            **FAKE_SINGLE_INFO,
            "chapters": [
                {"start_time": 0, "title": "Intro"},
                {"start_time": 120, "title": "Verse"},
                {"start_time": 240, "title": "Chorus"},
            ],
        }

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        captured_opts: list[dict] = []

        def fake_ydl_cls(opts):
            captured_opts.append(opts)
            return _make_ydl_mock(info)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            metadata = await downloader.fetch_metadata(_make_parsed_single())

        assert isinstance(metadata, TrackMetadata)
        assert metadata.video_id == VIDEO_ID
        assert metadata.title == "Never Gonna Give You Up"
        assert metadata.chapters == (
            (0, "Intro"),
            (120, "Verse"),
            (240, "Chorus"),
        )
        # Must be called with download=False — no audio fetched
        assert captured_opts, "yt-dlp was not invoked"
        # extract_info was called with download=False
        # (verified indirectly: mock's extract_info receives kwargs)

    async def test_fetch_metadata_no_chapters(self, tmp_path: Path) -> None:
        """Video without chapters returns TrackMetadata with chapters=None."""
        info = {**FAKE_SINGLE_INFO}  # no "chapters" key

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        with patch(
            "src.downloader.client.yt_dlp.YoutubeDL",
            side_effect=lambda opts: _make_ydl_mock(info),
        ):
            metadata = await downloader.fetch_metadata(_make_parsed_single())

        assert metadata.chapters is None
        assert metadata.video_id == VIDEO_ID

    async def test_fetch_metadata_filters_untitled_chapters(
        self, tmp_path: Path
    ) -> None:
        """Placeholder/empty chapter titles are filtered out (same as _build_result)."""
        info = {
            **FAKE_SINGLE_INFO,
            "chapters": [
                {"start_time": 0, "title": "Intro"},
                {"start_time": 60, "title": "<Untitled Chapter 1>"},
                {"start_time": 120, "title": "   "},
                {"start_time": 180, "title": "Real Chapter"},
            ],
        }

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        with patch(
            "src.downloader.client.yt_dlp.YoutubeDL",
            side_effect=lambda opts: _make_ydl_mock(info),
        ):
            metadata = await downloader.fetch_metadata(_make_parsed_single())

        assert metadata.chapters == (
            (0, "Intro"),
            (180, "Real Chapter"),
        )

    async def test_fetch_metadata_skips_download(self, tmp_path: Path) -> None:
        """extract_info must be called with download=False — no audio fetched."""
        info = {**FAKE_SINGLE_INFO}

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        extract_calls: list[dict] = []

        mock_ydl = _make_ydl_mock(info)

        def track_extract(url, **kwargs):
            extract_calls.append({"url": url, **kwargs})
            return info

        mock_ydl.extract_info.side_effect = track_extract

        with patch(
            "src.downloader.client.yt_dlp.YoutubeDL",
            side_effect=lambda opts: mock_ydl,
        ):
            await downloader.fetch_metadata(_make_parsed_single())

        assert len(extract_calls) == 1
        assert extract_calls[0].get("download") is False

    async def test_fetch_metadata_unavailable_message_raises_unavailable(
        self, tmp_path: Path
    ) -> None:
        """An unavailability-marker message raises VideoUnavailableError."""
        import yt_dlp

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        mock_ydl = MagicMock()
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError(
            "ERROR: This video is private"
        )

        with (
            patch(
                "src.downloader.client.yt_dlp.YoutubeDL",
                side_effect=lambda opts: mock_ydl,
            ),
            pytest.raises(VideoUnavailableError),
        ):
            await downloader.fetch_metadata(_make_parsed_single())

    async def test_fetch_metadata_transient_error_raises_generic(
        self, tmp_path: Path
    ) -> None:
        """Transient extractor/network errors raise generic DownloadError only.

        Metadata fetches commonly fail on proxy/rate-limit hiccups that
        should not be reported as "video unavailable" to the user.
        """
        import yt_dlp

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        mock_ydl = MagicMock()
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl.extract_info.side_effect = yt_dlp.utils.ExtractorError(
            "HTTP Error 429: Too Many Requests"
        )

        with (
            patch(
                "src.downloader.client.yt_dlp.YoutubeDL",
                side_effect=lambda opts: mock_ydl,
            ),
            pytest.raises(DownloadError) as excinfo,
        ):
            await downloader.fetch_metadata(_make_parsed_single())

        assert not isinstance(excinfo.value, VideoUnavailableError)

    async def test_fetch_metadata_passes_proxy_and_cookies(
        self, tmp_path: Path
    ) -> None:
        """fetch_metadata must honour proxy_url and cookies_file settings."""
        cookies = tmp_path / "cookies.txt"
        cookies.write_text("# test cookies")

        downloader = AudioDownloader(
            download_dir=tmp_path,
            max_file_size_bytes=10**9,
            proxy_url="http://proxy.test:8080",
            cookies_file=str(cookies),
        )

        captured_opts: list[dict] = []

        def fake_ydl_cls(opts):
            captured_opts.append(opts)
            return _make_ydl_mock(FAKE_SINGLE_INFO)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            await downloader.fetch_metadata(_make_parsed_single())

        assert captured_opts[0]["proxy"] == "http://proxy.test:8080"
        assert captured_opts[0]["cookiefile"] == str(cookies)

    async def test_fetch_metadata_does_not_run_postprocessors(
        self, tmp_path: Path
    ) -> None:
        """Minimal opts: no postprocessors / progress hooks / outtmpl."""
        downloader = AudioDownloader(download_dir=tmp_path, max_file_size_bytes=10**9)

        captured_opts: list[dict] = []

        def fake_ydl_cls(opts):
            captured_opts.append(opts)
            return _make_ydl_mock(FAKE_SINGLE_INFO)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            await downloader.fetch_metadata(_make_parsed_single())

        opts = captured_opts[0]
        assert "postprocessors" not in opts or not opts["postprocessors"]
        assert "progress_hooks" not in opts or not opts["progress_hooks"]
        assert "embedchapters" not in opts or not opts["embedchapters"]

    async def test_fetch_metadata_enables_node_js_runtime(self, tmp_path: Path) -> None:
        """fetch_metadata must set js_runtimes={'node': {}} like download().

        YouTube's JS signature challenge requires a Node.js runtime even
        for metadata-only extract_info calls on many videos.
        """
        downloader = AudioDownloader(download_dir=tmp_path, max_file_size_bytes=10**9)

        captured_opts: list[dict] = []

        def fake_ydl_cls(opts):
            captured_opts.append(opts)
            return _make_ydl_mock(FAKE_SINGLE_INFO)

        with patch("src.downloader.client.yt_dlp.YoutubeDL", side_effect=fake_ydl_cls):
            await downloader.fetch_metadata(_make_parsed_single())

        assert captured_opts[0].get("js_runtimes") == {"node": {}}

    async def test_fetch_metadata_geo_block_classified_as_unavailable(
        self, tmp_path: Path
    ) -> None:
        """YouTube's "has not made this video available" wording →
        VideoUnavailableError, not generic DownloadError.
        """
        import yt_dlp

        downloader = AudioDownloader(
            download_dir=tmp_path, max_file_size_bytes=10 * 1024 * 1024
        )

        mock_ydl = MagicMock()
        mock_ydl.__enter__ = MagicMock(return_value=mock_ydl)
        mock_ydl.__exit__ = MagicMock(return_value=False)
        mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError(
            "The uploader has not made this video available in your country"
        )

        with (
            patch(
                "src.downloader.client.yt_dlp.YoutubeDL",
                side_effect=lambda opts: mock_ydl,
            ),
            pytest.raises(VideoUnavailableError),
        ):
            await downloader.fetch_metadata(_make_parsed_single())
