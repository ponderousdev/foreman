#!/usr/bin/env bash
# agent-image-digest.sh — resolve IMAGE:TAG to an immutable digest pin and
# assert the published manifest is exactly linux/amd64.
#
# GHCR *tags* are mutable; the digest is the identity. The devcontainers/ci
# action exposes no digest output, so `publish` re-reads the manifest from the
# registry after the push. Prints `IMAGE:TAG@sha256:…` on stdout and, when
# $GITHUB_STEP_SUMMARY is set, appends a markdown pin line prefixed with the
# image name. Each matrix leg is its own job with its own step summary, so the
# bump path is: open the `publish (ai)` job's summary and copy the ref.
# Run via `task image:digest IMAGE=… TAG=…`.
set -euo pipefail

usage() {
    echo "usage: $0 IMAGE TAG" >&2
    exit 2
}

# Enumerate the platforms of a ref, one `os/arch` per line. The template must
# branch on the media type: .Manifest.Manifests is not a usable predicate on a
# single (non-index) manifest, and the single-manifest field is .Image.OS.
# Attestation manifests in an OCI index carry os "unknown" and are skipped.
# Field names verified against buildx 0.33 (`imagetools inspect --format`)
# for all three shapes: docker v2s2 manifest, OCI manifest, OCI index. The
# unit test replays recorded output and does NOT evaluate this template —
# re-verify against the live registry when buildx moves.
inspect_platforms() {
    _tmpl='{{if or (eq .Manifest.MediaType "application/vnd.oci.image.index.v1+json") (eq .Manifest.MediaType "application/vnd.docker.distribution.manifest.list.v2+json")}}{{range .Manifest.Manifests}}{{if ne .Platform.OS "unknown"}}{{.Platform.OS}}/{{.Platform.Architecture}}{{println}}{{end}}{{end}}{{else}}{{.Image.OS}}/{{.Image.Architecture}}{{println}}{{end}}'
    docker buildx imagetools inspect "$1" --format "$_tmpl"
}

# Collapse raw platform lines to a sorted, de-duplicated, space-separated set.
normalize_platforms() {
    sed '/^[[:space:]]*$/d' | LC_ALL=C sort -u | tr '\n' ' ' | sed 's/ *$//'
}

[ "$#" -eq 2 ] || usage
image="$1"
tag="$2"
[ -n "$image" ] && [ -n "$tag" ] || usage

ref="${image}:${tag}"

# buildx prints the digest without a trailing newline; strip whitespace anyway.
digest=$(docker buildx imagetools inspect "$ref" --format '{{.Manifest.Digest}}')
digest=$(printf '%s' "$digest" | tr -d '[:space:]')
if ! printf '%s\n' "$digest" | grep -Eq '^sha256:[0-9a-f]{64}$'; then
    echo "error: unexpected digest for ${ref}: '${digest}'" >&2
    exit 1
fi

pin="${image}@${digest}"

# Assert against the IMMUTABLE ref, not the tag: a concurrent push could move
# the tag between the two inspects, and we must vouch for the digest we print.
platforms=$(inspect_platforms "$pin" | normalize_platforms)
if [ "$platforms" != "linux/amd64" ]; then
    echo "error: ${pin} must be exactly linux/amd64; observed: ${platforms:-<none>}" >&2
    exit 1
fi

tagged_pin="${ref}@${digest}"
echo "$tagged_pin"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    echo "- \`${image}\` → \`${tagged_pin}\`" >>"$GITHUB_STEP_SUMMARY"
fi
