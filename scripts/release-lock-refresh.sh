#!/usr/bin/env bash
# Refresh uv.lock on the release-please branch after the version bump.
#
# release-please rewrites pyproject.toml's version but knows nothing about
# uv.lock, so every `uv run --locked` gate on the release PR fails with
# "The lockfile at `uv.lock` needs to be updated" until the lock records
# the new version. Runs in release.yml against a checkout of the release
# branch, in the same workflow run that created or force-updated it (the
# branch is regenerated on every push to main, so a one-off fix cannot
# stick). Commits and pushes only when the lock actually changed.
set -euo pipefail

branch="release-please--branches--main"

uv lock
if git diff --quiet -- uv.lock; then
    echo "uv.lock already current"
    exit 0
fi

version=$(uv version --short 2>/dev/null || echo "the release")
git config user.name "ponderousdev-ci[bot]"
git config user.email "ponderousdev-ci[bot]@users.noreply.github.com"
git add uv.lock
git commit -m "chore(release): refresh uv.lock for ${version}"
git push origin "HEAD:${branch}"
