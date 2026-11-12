"""YouTube JS challenge solving: runtime and solver scripts.

yt-dlp needs a supported JavaScript runtime plus the yt-dlp-ejs solver
scripts at the exact version it was released with. When either is missing it
only logs a warning and quietly loses formats, so these tests check the locked
environment for real and pin down that the post-deploy smoke check fails on a
broken one instead of passing.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from src.downloader.client import AudioDownloader, _js_runtimes

_SMOKE_TEST_PATH = Path(__file__).resolve().parent.parent / "scripts" / "smoke_test.py"


@pytest.fixture
def smoke() -> ModuleType:
    """scripts/smoke_test.py loaded as a module (scripts/ is not a package)."""
    spec = importlib.util.spec_from_file_location("smoke_test", _SMOKE_TEST_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_runtime(tmp_path: Path, name: str, version_output: str) -> Path:
    exe = tmp_path / name
    exe.write_text(f"#!/bin/sh\necho '{version_output}'\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    try:
        subprocess.run([str(exe)], check=True, capture_output=True)  # noqa: S603
    except PermissionError:
        pytest.skip("temp dir is mounted noexec")
    return exe


class TestLockedEnvironment:
    def test_locked_runtime_and_ejs_pass_the_smoke_check(
        self, smoke: ModuleType
    ) -> None:
        """The deno and yt-dlp-ejs from uv.lock are what yt-dlp accepts."""
        assert smoke.check_js_challenge_solver() == 0

    def test_ejs_matches_the_pin_in_yt_dlp_metadata(self, smoke: ModuleType) -> None:
        wanted = smoke._required_ejs_version()
        assert wanted is not None, "yt-dlp no longer pins yt-dlp-ejs in its extras"
        assert importlib.metadata.version("yt-dlp-ejs") == wanted

    def test_app_and_smoke_check_use_the_same_runtimes(
        self, smoke: ModuleType, tmp_path: Path
    ) -> None:
        """Download options and the smoke check name the same runtimes.

        fetch_metadata's options are covered in test_downloader.py.
        """
        downloader = AudioDownloader(tmp_path, max_file_size_bytes=10**9)
        loop = asyncio.new_event_loop()
        try:
            opts = downloader._build_opts(
                noplaylist=True, progress_callback=None, loop=loop
            )
        finally:
            loop.close()
        assert opts["js_runtimes"] == _js_runtimes() == smoke.JS_RUNTIMES
        assert _js_runtimes() is not _js_runtimes()


@pytest.mark.skipif(sys.platform == "win32", reason="uses a shell-script runtime")
class TestSmokeCheckFailsLoudly:
    def test_missing_runtime_fails(
        self, smoke: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = tmp_path / "no-such-deno"
        monkeypatch.setattr(smoke, "JS_RUNTIMES", {"deno": {"path": str(missing)}})
        assert smoke.check_js_challenge_solver() == 1

    def test_unsupported_runtime_version_fails(
        self,
        smoke: ModuleType,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Debian's node 20 is the case that slipped through before."""
        node = _fake_runtime(tmp_path, "node", "v20.19.2")
        monkeypatch.setattr(smoke, "JS_RUNTIMES", {"node": {"path": str(node)}})
        assert smoke.check_js_challenge_solver() == 1
        assert "node-20.19.2" in capsys.readouterr().out

    def test_supported_runtime_version_passes(
        self, smoke: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        node = _fake_runtime(tmp_path, "node", "v22.0.0")
        monkeypatch.setattr(smoke, "JS_RUNTIMES", {"node": {"path": str(node)}})
        assert smoke.check_js_challenge_solver() == 0

    def test_ejs_version_mismatch_fails(
        self, smoke: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(smoke.importlib.metadata, "version", lambda _: "0.0.1")
        assert smoke.check_js_challenge_solver() == 1

    def test_ejs_not_installed_fails(
        self, smoke: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def not_installed(name: str) -> str:
            raise importlib.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(smoke.importlib.metadata, "version", not_installed)
        assert smoke.check_js_challenge_solver() == 1

    def test_no_ejs_pin_in_yt_dlp_metadata_fails(
        self, smoke: ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(smoke.importlib.metadata, "requires", lambda _: [])
        assert smoke.check_js_challenge_solver() == 1
