# Deployment Guide

## One-time server setup

### 1. Generate a dedicated deploy SSH key (run on your local machine)

```bash
ssh-keygen -t ed25519 -f ~/.ssh/deploy_youtube_bot -C "github-actions-deploy" -N ""
```

### 2. Add the public key to the server

```bash
ssh-copy-id -i ~/.ssh/deploy_youtube_bot.pub your-server
```

### 3. Create the app directory and cache folder on the server

```bash
ssh your-server "mkdir -p ~/youtube-download-bot/cache && sudo chown 10001:10001 ~/youtube-download-bot/cache && chmod 750 ~/youtube-download-bot/cache"
```

> The bot container runs as UID 10001 (non-root). The cache directory must be owned by this UID so the container can read/write cached audio files.

### 4. Create the .env file on the server

```bash
ssh your-server "cat > ~/youtube-download-bot/.env" << 'EOF'
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_API_ID=your_api_id_here
TELEGRAM_API_HASH=your_api_hash_here
CACHE_DIR=./cache
CACHE_MAX_SIZE_GB=5.0
S3_ENABLED=false
MAX_FILE_SIZE_MB=2000
# Restrict access to your own Telegram user ID(s). Leaving this empty makes the
# bot PUBLIC — anyone can use your server's bandwidth/proxy. Only do that on purpose.
ALLOWED_USER_IDS=123456789
LOG_LEVEL=INFO
PLAYLIST_MAX_TRACKS=50
RATE_LIMIT_PER_MINUTE=5
GHCR_IMAGE=ghcr.io/your_github_username/youtube-download-bot:latest
EOF
```

Replace `your_github_username` (lowercase) with your actual GitHub username.

Lock down file permissions so other local users cannot read secrets:

```bash
ssh your-server "chmod 600 ~/youtube-download-bot/.env"
```

> `TELEGRAM_LOCAL_SERVER_URL` is intentionally omitted — it is injected automatically by `docker-compose.yml` as `http://telegram-bot-api:8081`.

---

## GitHub repository setup

### 1. Create a private repo and push

```bash
gh repo create youtube-download-bot --private --source=. --push
```

### 2. Register the self-hosted runner

The deploy job runs on the repo-specific self-hosted runner labels:

```yaml
runs-on: [self-hosted, Linux, X64, tg-audio-dl]
```

On the runner host, configure an SSH alias named `tg-audio-dl-vprojects`:

```sshconfig
Host tg-audio-dl-vprojects
    HostName 10.0.88.3
    User vladkarok
    IdentityFile ~/.ssh/tg_audio_dl_vprojects_deploy
    IdentitiesOnly yes
    StrictHostKeyChecking yes
```

The deployment workflow uses that alias directly from the runner. No deployment
host/user/key GitHub secrets are required.

Verify from the runner:

```bash
ssh tg-audio-dl-vprojects "hostname && cd ~/youtube-download-bot && docker compose config --services"
```

GHCR pulls use GitHub Actions' short-lived `github.token` with `packages: read`;
no long-lived GHCR PAT is needed for deploy.

---

## First deploy

After secrets are configured, create and push a version tag:

```bash
git tag v1.0.0 && git push origin v1.0.0
```

The workflow will:
1. Run tests + lint
2. Build the Docker image in GitHub Actions (not on your VPS)
3. Push it to `ghcr.io/YOUR_USERNAME/youtube-download-bot:latest`
4. Copy `docker-compose.yml` to the server
5. SSH in → pull new image → recreate bot container

---

## Subsequent deploys

Pushing a `v*` tag triggers a full deploy. Regular pushes to `master` only run tests and lint.

```bash
git tag v1.2.3 && git push origin v1.2.3
```

The image is pushed to GHCR with two tags:
- `ghcr.io/<owner>/youtube-download-bot:v1.2.3` — pinned version used for the deploy
- `ghcr.io/<owner>/youtube-download-bot:latest` — always points to the last release

---

## Proxy setup (required on datacenter IPs)

Datacenter IPs (Oracle Cloud, AWS, etc.) are blocked by YouTube at the IP level —
all player clients return `Sign in to confirm you're not a bot`. PO tokens, cookies,
and client spoofing do not help. The only reliable fix is routing yt-dlp traffic
through a residential IP proxy.

### Option A: Residential proxy service

Buy a residential/mobile proxy from a provider (e.g. smartproxy, brightdata, oxylabs).
Most offer SOCKS5 proxies with rotating residential IPs.

Add to `.env` on the server:

```bash
PROXY_URL=socks5://user:pass@proxy-host:1080
```

### Option B: Self-hosted proxy via home connection

Run a SOCKS5 proxy on your home network (e.g. Raspberry Pi, old laptop, router)
and tunnel into it from the server.

**On your home machine** — install and start Dante (SOCKS5 server):

```bash
sudo apt install dante-server
# Configure /etc/danted.conf, then:
sudo systemctl enable danted && sudo systemctl start danted
```

**Or use SSH tunnel** (simpler, no software needed on home machine):

```bash
# On the server — creates a SOCKS5 proxy at localhost:1080 through your home IP
ssh -D 1080 -f -N -o ServerAliveInterval=60 user@your-home-ip
```

Add to `.env`:

```bash
# If running OUTSIDE Docker (bare-metal):
PROXY_URL=socks5://127.0.0.1:1080

# If running INSIDE Docker (docker-compose):
# 127.0.0.1 is the container itself — use the host alias instead:
PROXY_URL=socks5://host.docker.internal:1080
```

### Option C: WireGuard tunnel

Set up WireGuard between the server and a home device. Route only YouTube traffic
through the tunnel. This is the most robust self-hosted option.

See [docs/WIREGUARD_PROXY_SETUP.md](docs/WIREGUARD_PROXY_SETUP.md) for step-by-step instructions.

### Verify the proxy works

```bash
ssh your-server "docker exec youtube-download-bot-bot-1 python3 /app/scripts/smoke_test.py"
```

### Restart after changing proxy

```bash
ssh your-server "cd ~/youtube-download-bot && docker compose up -d --force-recreate bot"
```

---

## Monitoring

```bash
# View live logs
ssh your-server "docker compose -f ~/youtube-download-bot/docker-compose.yml logs -f bot"

# Check container status
ssh your-server "docker compose -f ~/youtube-download-bot/docker-compose.yml ps"

# Check resource usage
ssh your-server "docker stats --no-stream"
```

---

## Resource limits (tuned for 1 GB RAM VPS)

| Container | RAM limit | CPU limit |
|---|---|---|
| `telegram-bot-api` | 256 MB | 0.5 cores |
| `bot` | 512 MB | 1.5 cores |

Combined hard cap: 768 MB RAM. The 8 GB swap absorbs ffmpeg/download spikes.
