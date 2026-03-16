#!/usr/bin/env python3
"""
Smoke test for the YouTube download pipeline.

Run directly in the container to diagnose issues without deploying:
    docker exec youtube-download-bot-bot-1 python3 /app/scripts/smoke_test.py

Or from the host:
    ssh your-server "docker exec youtube-download-bot-bot-1 python3 /app/scripts/smoke_test.py"
"""

import os
import shutil
import sys

TEST_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # "Me at the zoo" — first YT video, always public

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m!\033[0m"


def check(label: str, ok: bool, detail: str = "") -> bool:
    icon = PASS if ok else FAIL
    print(f"  {icon} {label}" + (f": {detail}" if detail else ""))
    return ok


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
        print(f"{PASS} All checks passed — bot should be working.\n")
    else:
        print(f"{FAIL} {failures} check(s) failed — see above.\n")
    return failures


if __name__ == "__main__":
    sys.exit(main())
