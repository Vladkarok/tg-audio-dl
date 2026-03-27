#!/usr/bin/env python3
"""
Smoke test for the YouTube download pipeline.

Run directly in the container to diagnose issues without deploying:
    docker exec youtube-download-bot-bot-1 python3 /app/scripts/smoke_test.py

Or from the host:
    ssh your-server "docker exec youtube-download-bot-bot-1 \
        python3 /app/scripts/smoke_test.py"
"""

import os
import shutil
import socket
import sys
import urllib.parse
import urllib.request

# "Me at the zoo" — first YT video, always public
TEST_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"

ICON_OK = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m!\033[0m"


def check(label: str, ok: bool, detail: str = "") -> bool:
    icon = ICON_OK if ok else FAIL
    print(f"  {icon} {label}" + (f": {detail}" if detail else ""))
    return ok


_IP_CHECK_URL = "https://api.ipify.org"
_IP_CHECK_TIMEOUT = 10


def _fetch_ip_direct() -> str:
    """Fetch WAN IP without any proxy (urllib ignores PROXY_URL — it's yt-dlp only)."""
    req = urllib.request.Request(_IP_CHECK_URL, headers={"User-Agent": "curl/8"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=_IP_CHECK_TIMEOUT) as resp:  # noqa: S310
        return resp.read().decode().strip()


def _fetch_ip_via_socks5(proxy_url: str) -> str:
    """Fetch WAN IP through the SOCKS5 proxy using a raw socket tunnel."""
    parsed = urllib.parse.urlparse(proxy_url)
    proxy_host = parsed.hostname or ""
    proxy_port = parsed.port or 1080

    # Open SOCKS5 tunnel to api.ipify.org:443 — but we use port 80 (plain HTTP)
    # to avoid TLS complexity in a raw socket; ipify supports both.
    target_host = "api.ipify.org"
    target_port = 80
    target_bytes = target_host.encode()

    with socket.create_connection(
        (proxy_host, proxy_port), timeout=_IP_CHECK_TIMEOUT
    ) as s:
        # Greeting
        s.sendall(b"\x05\x01\x00")
        reply = s.recv(2)
        if len(reply) < 2 or reply[1] != 0x00:
            raise OSError(f"SOCKS5 auth negotiation failed: {reply!r}")

        # CONNECT request (ATYP=3 domain name)
        s.sendall(
            b"\x05\x01\x00\x03"
            + bytes([len(target_bytes)])
            + target_bytes
            + target_port.to_bytes(2, "big")
        )
        # Read response (at least 10 bytes for IPv4 reply)
        resp = s.recv(256)
        if len(resp) < 2 or resp[1] != 0x00:
            raise OSError(f"SOCKS5 CONNECT failed: {resp!r}")

        # Send minimal HTTP/1.0 GET (no keep-alive, so server closes after response)
        s.sendall(
            (
                f"GET / HTTP/1.0\r\nHost: {target_host}\r\nUser-Agent: curl/8\r\n\r\n"
            ).encode()
        )
        raw = b""
        while chunk := s.recv(4096):
            raw += chunk

    body = raw.split(b"\r\n\r\n", 1)[-1].decode().strip()
    if not body:
        raise OSError("empty response from ipify")
    return body


def _check_egress_differs(proxy_url: str) -> int:
    """Return 0 (pass) if proxy egress IP differs from direct IP, 1 (fail) otherwise."""
    direct_ip = ""
    proxy_ip = ""
    try:
        direct_ip = _fetch_ip_direct()
    except Exception as exc:
        check("egress IP check", False, f"direct fetch failed: {exc}")
        return 1

    try:
        proxy_ip = _fetch_ip_via_socks5(proxy_url)
    except Exception as exc:
        check("egress IP check", False, f"proxy fetch failed: {exc}")
        return 1

    if direct_ip == proxy_ip:
        check(
            "egress IP differs from direct",
            False,
            f"both are {direct_ip} — proxy is not routing through a different path",
        )
        return 1

    check(
        "egress IP differs from direct",
        True,
        f"direct={direct_ip}  proxy={proxy_ip}",
    )
    return 0


def main() -> int:
    print("\n=== tg-audio-dl smoke test ===\n")
    failures = 0

    # --- 1. Node.js available ---
    node = shutil.which("node")
    if not check("Node.js installed", node is not None, node or "not found"):
        failures += 1

    # --- 2. ffmpeg available ---
    ffmpeg = shutil.which("ffmpeg")
    if not check("ffmpeg installed", ffmpeg is not None, ffmpeg or "not found"):
        failures += 1

    # --- 3. yt-dlp importable ---
    try:
        import yt_dlp

        check("yt-dlp importable", True, yt_dlp.version.__version__)
    except ImportError as e:
        check("yt-dlp importable", False, str(e))
        failures += 1
        print("\nCannot continue without yt-dlp.\n")
        return failures

    # --- 4. Proxy ---
    proxy_url = os.environ.get("PROXY_URL", "").strip()
    if proxy_url:
        check("PROXY_URL configured", True, proxy_url.split("@")[-1])
    else:
        check("PROXY_URL configured", False, "not set")
        print(f"  {WARN} Datacenter IPs need a proxy. Set PROXY_URL in .env")

    # --- 4a. SOCKS5 handshake (only for socks5:// proxies) ---
    if proxy_url and urllib.parse.urlparse(proxy_url).scheme in (
        "socks5",
        "socks5h",
    ):
        parsed = urllib.parse.urlparse(proxy_url)
        proxy_host = parsed.hostname or ""
        proxy_port = parsed.port or 1080
        socks5_ok = False
        socks5_detail = ""
        try:
            with socket.create_connection((proxy_host, proxy_port), timeout=5) as sock:
                # SOCKS5 greeting: VER=5, NMETHODS=1, METHOD=0 (no auth)
                sock.sendall(b"\x05\x01\x00")
                reply = sock.recv(2)
                if len(reply) < 2:
                    socks5_detail = f"short reply ({reply!r})"
                elif reply[0] != 0x05:
                    socks5_detail = f"unexpected VER byte: 0x{reply[0]:02x}"
                elif reply[1] != 0x00:
                    socks5_detail = (
                        f"server chose auth method 0x{reply[1]:02x}, expected 0x00"
                    )
                else:
                    socks5_ok = True
                    socks5_detail = f"{proxy_host}:{proxy_port} accepted no-auth"
        except OSError as exc:
            socks5_detail = str(exc)
        if not check("SOCKS5 handshake", socks5_ok, socks5_detail):
            failures += 1

    # --- 4b. Egress IP differs from direct IP ---
    if proxy_url and urllib.parse.urlparse(proxy_url).scheme in ("socks5", "socks5h"):
        failures += _check_egress_differs(proxy_url)

    # --- 5. Extraction check (no download) ---
    print("\n  Testing yt-dlp extract (no download)...")
    try:
        opts: dict = {
            "quiet": True,
            "no_warnings": False,
            "skip_download": True,
            "js_runtimes": {"node": {}},
        }
        if proxy_url:
            opts["proxy"] = proxy_url

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(TEST_URL, download=False)

        title = info.get("title", "unknown")
        fmts = [f for f in info.get("formats", []) if f.get("vcodec") == "none"]
        check("metadata extraction", True, f'"{title}"')
        check("audio formats available", len(fmts) > 0, f"{len(fmts)} formats")

    except Exception as e:
        msg = str(e)[:120]
        if "Sign in to confirm" in msg:
            check("metadata extraction", False, "bot detection")
            if not proxy_url:
                print(f"  {WARN} Set PROXY_URL to a residential proxy in .env")
            else:
                print(f"  {WARN} Proxy may also be a datacenter IP. Use residential.")
        else:
            check("metadata extraction", False, msg)
        failures += 1

    # --- Summary ---
    print()
    if failures == 0:
        print(f"{ICON_OK} All checks passed — bot should be working.\n")
    else:
        print(f"{FAIL} {failures} check(s) failed — see above.\n")
    return failures


if __name__ == "__main__":
    sys.exit(main())
