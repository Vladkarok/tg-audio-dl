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
ssh your-server "mkdir -p ~/youtube-download-bot/cache && sudo chown -R \$(whoami):\$(whoami) ~/youtube-download-bot"
```

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
GHCR_IMAGE=ghcr.io/YOUR_GITHUB_USERNAME/youtube-download-bot:v1.0.0
EOF
```

Replace `YOUR_GITHUB_USERNAME` with your actual GitHub username.

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
