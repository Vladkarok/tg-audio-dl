#!/usr/bin/env bash
set -euo pipefail

target_alias="${DEPLOY_SSH_ALIAS:-tg-audio-dl-vprojects}"
app_dir="${DEPLOY_APP_DIR:-/home/vladkarok/youtube-download-bot}"

if [[ -z "${REPO_OWNER:-}" ]]; then
  echo "REPO_OWNER is required" >&2
  exit 1
fi

if [[ -z "${GHCR_IMAGE:-}" ]]; then
  echo "GHCR_IMAGE is required" >&2
  exit 1
fi

quote() {
  printf "%q" "$1"
}

ssh "$target_alias" "mkdir -p $(quote "$app_dir")/scripts $(quote "$app_dir")/cache"
scp docker-compose.yml "$target_alias:$(quote "$app_dir")/docker-compose.yml"
scp scripts/smoke_test.py "$target_alias:$(quote "$app_dir")/scripts/smoke_test.py"

ssh "$target_alias" \
  "APP_DIR=$(quote "$app_dir") GHCR_TOKEN=$(quote "${GHCR_TOKEN:-}") REPO_OWNER=$(quote "$REPO_OWNER") GHCR_IMAGE=$(quote "$GHCR_IMAGE") bash -s" <<'REMOTE'
set -euo pipefail

cd "$APP_DIR"

if [[ -n "$GHCR_TOKEN" ]]; then
  echo "$GHCR_TOKEN" | docker login ghcr.io -u "$REPO_OWNER" --password-stdin
fi

sed -i "s|^GHCR_IMAGE=.*|GHCR_IMAGE=${GHCR_IMAGE}|" .env
grep -q '^GHCR_IMAGE=' .env || echo "GHCR_IMAGE=${GHCR_IMAGE}" >> .env

docker compose pull bot
docker compose up -d --force-recreate bot
docker image prune -af

docker exec -i youtube-download-bot-bot-1 python3 - < "$APP_DIR/scripts/smoke_test.py"
REMOTE
