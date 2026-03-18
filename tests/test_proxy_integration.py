"""Integration tests for proxy connectivity.

Spins up an in-process SOCKS5 server using only the stdlib, then verifies
that yt-dlp opts are correctly wired and that a real socket handshake
succeeds through the proxy.
"""

import socket
import socketserver
import threading

import pytest

# ---------------------------------------------------------------------------
# Minimal SOCKS5 echo server (no auth, CONNECT only → immediate close)
# ---------------------------------------------------------------------------


class _Socks5Handler(socketserver.BaseRequestHandler):
    """Minimal SOCKS5 server that accepts greeting + CONNECT, then closes.

    Enough to verify the proxy URL plumbing end-to-end without making
    real outbound connections.
    """

    def handle(self) -> None:
        sock: socket.socket = self.request
        sock.settimeout(5)
        try:
            # --- Greeting ---
            data = sock.recv(256)
            if len(data) < 3 or data[0] != 0x05:
                return
            # Accept no-auth (method 0x00)
            sock.sendall(b"\x05\x00")

            # --- CONNECT request ---
            data = sock.recv(256)
            if len(data) < 7 or data[0] != 0x05 or data[1] != 0x01:
                return
            # Reply: success (VER=5, REP=0, RSV=0, ATYP=1, BND.ADDR=0.0.0.0, BND.PORT=0)
            sock.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        except OSError:
            pass


class _Socks5Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def socks5_server():
    """Start a local SOCKS5 server on a random port, yield (host, port)."""
    try:
        server = _Socks5Server(("127.0.0.1", 0), _Socks5Handler)
    except PermissionError as exc:
        pytest.skip(f"local socket binding not permitted in this environment: {exc}")
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield host, port
    server.shutdown()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSocks5Handshake:
    """Verify raw SOCKS5 greeting works against our in-process server."""

    def test_greeting_accepted(self, socks5_server: tuple[str, int]) -> None:
        host, port = socks5_server
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(b"\x05\x01\x00")
            reply = sock.recv(2)
            assert len(reply) == 2
            assert reply[0] == 0x05  # VER
            assert reply[1] == 0x00  # no-auth accepted

    def test_connect_request_accepted(self, socks5_server: tuple[str, int]) -> None:
        host, port = socks5_server
        with socket.create_connection((host, port), timeout=5) as sock:
            # Greeting
            sock.sendall(b"\x05\x01\x00")
            sock.recv(2)
            # CONNECT to 1.2.3.4:80
            sock.sendall(
                b"\x05\x01\x00\x01"  # VER, CMD=CONNECT, RSV, ATYP=IPv4
                b"\x01\x02\x03\x04"  # DST.ADDR
                b"\x00\x50"  # DST.PORT = 80
            )
            reply = sock.recv(10)
            assert len(reply) >= 4
            assert reply[0] == 0x05  # VER
            assert reply[1] == 0x00  # REP = success


class TestYtdlpProxyOpts:
    """Verify AudioDownloader properly passes proxy_url to yt-dlp opts."""

    def test_proxy_url_in_ytdlp_opts(self, tmp_path) -> None:
        import asyncio

        from src.downloader.client import AudioDownloader

        proxy = "socks5://127.0.0.1:1080"
        dl = AudioDownloader(
            download_dir=tmp_path,
            max_file_size_bytes=100 * 1024 * 1024,
            proxy_url=proxy,
        )
        loop = asyncio.new_event_loop()
        opts = dl._build_opts(noplaylist=True, progress_callback=None, loop=loop)
        assert opts["proxy"] == proxy

    def test_no_proxy_url_omitted(self, tmp_path) -> None:
        import asyncio

        from src.downloader.client import AudioDownloader

        dl = AudioDownloader(
            download_dir=tmp_path,
            max_file_size_bytes=100 * 1024 * 1024,
            proxy_url=None,
        )
        loop = asyncio.new_event_loop()
        opts = dl._build_opts(noplaylist=True, progress_callback=None, loop=loop)
        assert "proxy" not in opts


class TestProxyConfig:
    """Verify PROXY_URL config validation."""

    def test_socks5_proxy_accepted(self, monkeypatch) -> None:
        from src.config import Settings

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        monkeypatch.setenv("PROXY_URL", "socks5://127.0.0.1:1080")
        s = Settings()  # type: ignore[call-arg]
        assert s.PROXY_URL == "socks5://127.0.0.1:1080"

    def test_http_proxy_accepted(self, monkeypatch) -> None:
        from src.config import Settings

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        monkeypatch.setenv("PROXY_URL", "http://proxy.example.com:8080")
        s = Settings()  # type: ignore[call-arg]
        assert s.PROXY_URL == "http://proxy.example.com:8080"

    def test_invalid_proxy_rejected(self, monkeypatch) -> None:
        from pydantic import ValidationError

        from src.config import Settings

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        monkeypatch.setenv("PROXY_URL", "ftp://invalid")
        with pytest.raises(ValidationError, match="PROXY_URL"):
            Settings()  # type: ignore[call-arg]

    def test_empty_proxy_is_none(self, monkeypatch) -> None:
        from src.config import Settings

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
        monkeypatch.setenv("PROXY_URL", "")
        s = Settings()  # type: ignore[call-arg]
        assert s.PROXY_URL is None


class TestProxySocketConnectivity:
    """End-to-end: verify the full SOCKS5 handshake flow through our server."""

    async def test_async_proxy_reachable(self, socks5_server: tuple[str, int]) -> None:
        """Simulate what the smoke_test does: async socket connect + greeting."""
        host, port = socks5_server

        def _handshake() -> bool:
            with socket.create_connection((host, port), timeout=5) as sock:
                sock.sendall(b"\x05\x01\x00")
                reply = sock.recv(2)
                return len(reply) == 2 and reply[0] == 0x05 and reply[1] == 0x00

        # A real worker thread is unnecessary here; the test is only verifying
        # the end-to-end SOCKS5 greeting against the in-process server.
        result = _handshake()
        assert result is True

    def test_unreachable_proxy_fails_gracefully(self) -> None:
        """Connecting to a closed port should raise OSError, not hang."""
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", 19999), timeout=1)
