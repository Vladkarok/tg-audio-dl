# tg-audio-dl

A self-hosted Telegram bot that downloads audio from YouTube and SoundCloud and sends it as an audio file.

## Features

- **YouTube & SoundCloud** — single tracks, playlists, Shorts, radio mixes, SoundCloud sets
- **2 GB uploads** via local Telegram Bot API server (bypasses the 50 MB limit)
- **Smart caching** — disk LRU cache + optional S3; Telegram `file_id` resend for instant repeat requests
- **Live progress** — step-by-step status message (downloading → converting → uploading)
- **Native M4A** — no re-encoding, embedded thumbnail and metadata
- **Hardened Docker** — rootless (UID 10001), `cap_drop: ALL`, read-only filesystem, resource limits
- **Access control** — user allowlist + per-user rate limiting

## Quick start

```bash
cp .env.example .env
# fill in TELEGRAM_BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH
# IMPORTANT: set ALLOWED_USER_IDS to restrict access (empty = public bot)
mkdir -p cache && sudo chown 10001:10001 cache && chmod 750 cache
docker compose up -d
```

## Deployment

See [DEPLOY.md](DEPLOY.md) for VPS deployment with GitHub Actions CI/CD.
Deploy by pushing a version tag — the image is published to GHCR with both a pinned version tag and `latest`:
```bash
git tag v1.2.3 && git push origin v1.2.3
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Bot token from @BotFather |
| `TELEGRAM_API_ID` | — | From my.telegram.org/apps |
| `TELEGRAM_API_HASH` | — | From my.telegram.org/apps |
| `TELEGRAM_LOCAL_SERVER_URL` | `None` | Local Bot API server URL (set automatically in Docker) |
| `CACHE_DIR` | `./cache` | Directory for cached audio files |
| `CACHE_MAX_SIZE_GB` | `5.0` | Max disk cache size |
| `MAX_FILE_SIZE_MB` | `2000` | Max file size to send |
| `ALLOWED_USER_IDS` | `[]` | Allowlist (empty = public); comma-separated or JSON array |
| `RATE_LIMIT_PER_MINUTE` | `5` | Requests per user per minute |
| `PLAYLIST_MAX_TRACKS` | `50` | Max tracks from a playlist |
| `CHAPTER_PAGES_ENABLED` | `true` | Render overflowing chapter lists as paginated pages with inline nav buttons (and enable `/chapters`). Set `false` for the legacy multi-message chapter index |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG/INFO/WARNING/ERROR/CRITICAL) |
| `PROXY_URL` | `None` | SOCKS5/HTTP proxy for yt-dlp (required on datacenter IPs); see [DEPLOY.md](DEPLOY.md) |
| `S3_ENABLED` | `false` | Enable S3 secondary cache |
| `S3_BUCKET` | — | S3 bucket name (required if S3_ENABLED=true) |
| `AWS_ACCESS_KEY_ID` | — | AWS credentials for S3 |
| `AWS_SECRET_ACCESS_KEY` | — | AWS credentials for S3 |
| `AWS_REGION` | `us-east-1` | AWS region for S3 |

## Tech stack

Python 3.13 · python-telegram-bot · yt-dlp · pydantic-settings · boto3 · mutagen · Docker
