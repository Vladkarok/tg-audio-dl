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
ssh your-server "mkdir -p ~/youtube-download-bot/cache && chmod 777 ~/youtube-download-bot/cache"
```

> The bot container runs as UID 10001 (non-root). Using `chmod 777` allows both the host user and the container to read/write the cache directory regardless of UID mapping differences between host and container.

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
ALLOWED_USER_IDS=[]
LOG_LEVEL=INFO
PLAYLIST_MAX_TRACKS=50
RATE_LIMIT_PER_MINUTE=5
GHCR_IMAGE=ghcr.io/your_github_username/youtube-download-bot:v1.0.0
# Uncomment and set after following the "YouTube cookies" section below:
# COOKIES_FILE=/home/ubuntu/youtube-download-bot/cookies.txt
EOF
```

Replace `your_github_username` (lowercase) with your actual GitHub username and `v1.0.0` with the version you're deploying.

> `TELEGRAM_LOCAL_SERVER_URL` is intentionally omitted — it is injected automatically by `docker-compose.yml` as `http://telegram-bot-api:8081`.

---

## GitHub repository setup

### 1. Create a private repo and push

```bash
gh repo create youtube-download-bot --private --source=. --push
```

### 2. Add GitHub Secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret name | Value |
|---|---|
| `DEPLOY_SSH_KEY` | Contents of `~/.ssh/deploy_youtube_bot` (private key) |
| `DEPLOY_HOST` | Your VPS IP address |
| `DEPLOY_PORT` | Your VPS SSH port |
| `DEPLOY_USER` | Your VPS SSH username |
| `GHCR_TOKEN` | GitHub PAT with **`read:packages`** scope |

To get the private key value:
```bash
cat ~/.ssh/deploy_youtube_bot
```

To create the PAT: go to **GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)** → generate a new token with only the **`read:packages`** scope selected.

> **Note:** Fine-grained PATs do not support GitHub Container Registry (GHCR). You must use a classic PAT.

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

## YouTube cookies (required on datacenter IPs)

Datacenter IPs (Oracle Cloud, AWS, etc.) are often flagged by YouTube and will get
`Sign in to confirm you're not a bot` errors. Fix by passing browser cookies to yt-dlp.

### 1. Export cookies from your browser

Install the **"Get cookies.txt LOCALLY"** extension (Chrome/Firefox), log into YouTube,
then export cookies for `youtube.com` → save as `cookies.txt`.

### 2. Copy to the server and set permissions

```bash
scp cookies.txt your-server:~/youtube-download-bot/cookies.txt
ssh your-server "chmod 666 ~/youtube-download-bot/cookies.txt"
```

> `chmod 666` is required — yt-dlp updates the cookies file after each request, and the container runs as a different UID than the host user.

### 3. Add to `.env` on the server

```bash
COOKIES_FILE=/home/ubuntu/youtube-download-bot/cookies.txt
```

> `COOKIES_FILE` is the host path to your cookies file. Docker mounts it into the container at `/app/cookies.txt` — the bot detects it automatically, no other config needed.

### 4. Restart the bot

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
