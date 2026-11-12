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

Pushing a `v*` tag triggers a full deploy. Regular pushes to `master` (and
merged PRs) only run tests, lint and an image build check; they reach
production only when someone tags. The yt-dlp updater below tags on its own
only when `master` has nothing else unreleased.

```bash
git tag v1.2.3 && git push origin v1.2.3
```

The image is pushed to GHCR with two tags:
- `ghcr.io/<owner>/youtube-download-bot:v1.2.3` — pinned version used for the deploy
- `ghcr.io/<owner>/youtube-download-bot:latest` — always points to the last release

### Automated yt-dlp releases

`update-yt-dlp.yml` checks PyPI every 4 hours. When a newer yt-dlp release has
passed the one-day cooldown, it bumps `uv.lock` and commits the bump to
`master`. What happens next depends on what else is on `master`:

- **Nothing unreleased since the last tag** (only the updater's own earlier
  bumps, if any): it pushes the next patch tag and deploys it, with nobody
  involved.
- **Anything else since the last tag** (a merged PR, a hand-made commit, a
  merge commit), or when a newer `v*` tag exists that is not on `master`: it
  pushes the bump but no tag, writes the unreleased commits and a suggested
  tag to the run summary, and fails the run with "Manual release needed".
  GitHub sends failure notifications for a scheduled run to whoever last
  edited the `cron` line in the workflow (for a manual run, whoever started
  it), so make sure that is you. Production keeps the old yt-dlp until you
  tag `master` yourself, which ships the bump together with the work you
  merged:

  ```bash
  git pull && git tag v1.2.3 && git push origin v1.2.3
  ```

  Only the run that commits the bump fails. Later runs find the lock already
  current and pass, so one email per held yt-dlp version.

So merging a PR no longer means "deploy at the next yt-dlp release". It
means "this waits until I tag". The cost: while unreleased work sits on
`master`, a yt-dlp fix waits for your tag too. Tag promptly, or keep `master`
releasable. The rules live in `scripts/release_decision.sh` and are tested by
`tests/test_release_decision.py`.

The updater's pushes use the default `GITHUB_TOKEN`, and GitHub does not start
workflow runs for `GITHUB_TOKEN` pushes, so its tag alone deploys nothing (and
its untagged bump commits get no CI run on `master`; CI runs when you tag).
The updater therefore calls `ci.yml` directly as a reusable workflow
(`build_tag` input), which builds and deploys the tag it just pushed. Do not
"simplify" that call away: without it the bumped yt-dlp is tagged but never
reaches the server.

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
