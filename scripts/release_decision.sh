#!/usr/bin/env bash
# Decide whether update-yt-dlp.yml may tag and deploy HEAD on its own.
#
# Run in the repo before the updater commits its new bump. It releases
# unattended only when everything since the last release tag is the updater's
# own earlier yt-dlp bumps (or nothing at all). Anything else on master is work
# a person merged but has not released, and shipping it stays their call.
#
# Prints key=value lines for $GITHUB_OUTPUT:
#   decision=release|hold
#   reason=<short text>
#   last_tag=<highest vX.Y.Z tag reachable from HEAD, or empty>
#   next_tag=<last_tag with PATCH+1, or empty when there is no last_tag>
set -euo pipefail

BOT_EMAIL="github-actions[bot]@users.noreply.github.com"
BUMP_SUBJECT_RE='^chore: bump yt-dlp '
SEMVER_RE='^v([0-9]+)\.([0-9]+)\.([0-9]+)$'

emit() { printf '%s=%s\n' "$1" "$2"; }

# Highest release tag, by version rather than by distance from HEAD, so two
# tags on one commit or a closer tag on a merged side branch do not matter.
highest_tag() {
  git tag --list 'v*' --sort=-v:refname "$@" | grep -E "$SEMVER_RE" | head -n1 || true
}

last_tag=$(highest_tag --merged HEAD)
emit last_tag "$last_tag"

if [[ -z $last_tag ]]; then
  emit next_tag ""
  emit decision hold
  emit reason "no vX.Y.Z tag reachable from HEAD, so there is no version to bump"
  exit 0
fi

[[ $last_tag =~ $SEMVER_RE ]]
next_tag="v${BASH_REMATCH[1]}.${BASH_REMATCH[2]}.$((BASH_REMATCH[3] + 1))"
emit next_tag "$next_tag"

newest_anywhere=$(highest_tag)
if [[ $newest_anywhere != "$last_tag" ]]; then
  emit decision hold
  emit reason "$newest_anywhere is newer than $last_tag but not on master"
  exit 0
fi

others=0
while read -r sha; do
  # %P lists parent hashes: exactly one means not a merge.
  IFS=$'\x1f' read -r parents email subject \
    < <(git log -1 --format='%P%x1f%ae%x1f%s' "$sha")
  files=$(git diff-tree --no-commit-id --name-only -r "$sha")
  if [[ $parents != *" "* ]] && [[ $email == "$BOT_EMAIL" ]] \
    && [[ $subject =~ $BUMP_SUBJECT_RE ]] && [[ $files == uv.lock ]]; then
    continue
  fi
  others=$((others + 1))
done < <(git rev-list "$last_tag..HEAD")

if ((others > 0)); then
  emit decision hold
  emit reason "$others commit(s) since $last_tag are not yt-dlp bumps"
  exit 0
fi

emit decision release
emit reason "nothing but yt-dlp bumps since $last_tag"
