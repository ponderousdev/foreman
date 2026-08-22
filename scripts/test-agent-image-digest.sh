#!/usr/bin/env bash
# test-agent-image-digest.sh — unit-test agent-image-digest.sh against a fake
# `docker` first on PATH that replays RECORDED `buildx imagetools inspect
# --format` output, keyed by the format string. Covers: a single docker v2s2
# manifest, an OCI index carrying attestation manifests, a multi-arch index
# (must fail), malformed digests (must fail), the step-summary line, and the
# assertion that the platform check is made against the immutable digest ref
# rather than the mutable tag. The recorded outputs were captured from buildx
# 0.33 against ghcr.io/ponderousdev/foreman-devcontainer (single manifest) and
# docker.io/library/alpine:3.20 (index, 8 unknown/unknown attestation entries
# the template drops, two linux/arm variants `sort -u` collapses).
# Run via `task test:image:digest`.
set -euo pipefail
cd "$(dirname "$0")/.."
digest_sh="$PWD/scripts/agent-image-digest.sh"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin"
calls="$work/calls.log"

h8="33333333"
D_GOOD="sha256:$h8$h8$h8$h8$h8$h8$h8$h8"
IMG="ghcr.io/ponderousdev/foreman-devcontainer"
TAG="sha-abcdef01"

fail() {
    echo "TEST FAIL: $*" >&2
    exit 1
}

# The shim: `docker buildx imagetools inspect REF --format TMPL`. It logs every
# ref it is asked about and answers from FAKE_DIGEST / FAKE_PLATFORMS, choosing
# by which template it was handed.
cat >"$work/bin/docker" <<'SHIM'
#!/usr/bin/env bash
set -euo pipefail
[ "$1" = "buildx" ] && [ "$2" = "imagetools" ] && [ "$3" = "inspect" ] || {
    echo "shim: unexpected argv: $*" >&2
    exit 99
}
ref="$4"
tmpl="$6"
case "$tmpl" in
*Manifest.Digest*)
    echo "digest $ref" >>"$FAKE_CALLS"
    printf '%s' "$FAKE_DIGEST"
    ;;
*Platform.OS*)
    echo "platforms $ref" >>"$FAKE_CALLS"
    printf '%s' "$FAKE_PLATFORMS"
    ;;
*)
    echo "shim: unexpected template: $tmpl" >&2
    exit 99
    ;;
esac
SHIM
chmod +x "$work/bin/docker"

# run ARGS... -> sets $RC and $ALL (combined output). GITHUB_STEP_SUMMARY is
# always overridden (CI sets it for every step) so the test never writes fake
# pin lines into a real job summary. Deliberately NOT called
# in a command substitution: a subshell would discard both globals.
run() {
    : >"$calls"
    RC=0
    ALL=$(PATH="$work/bin:$PATH" FAKE_CALLS="$calls" \
        FAKE_DIGEST="$FAKE_DIGEST" FAKE_PLATFORMS="$FAKE_PLATFORMS" \
        GITHUB_STEP_SUMMARY="${TEST_SUMMARY:-}" \
        "$digest_sh" "$@" 2>&1) || RC=$?
}
run_stdout() {
    : >"$calls"
    PATH="$work/bin:$PATH" FAKE_CALLS="$calls" \
        FAKE_DIGEST="$FAKE_DIGEST" FAKE_PLATFORMS="$FAKE_PLATFORMS" \
        GITHUB_STEP_SUMMARY="${TEST_SUMMARY:-}" \
        "$digest_sh" "$@" 2>/dev/null
}

echo "==> a single docker v2s2 linux/amd64 manifest resolves and prints the pin"
FAKE_DIGEST="$D_GOOD"
FAKE_PLATFORMS="linux/amd64
"
[ "$(run_stdout "$IMG" "$TAG")" = "$IMG:$TAG@$D_GOOD" ] ||
    fail "expected $IMG:$TAG@$D_GOOD"

echo "==> the platform assertion is made against the immutable digest ref"
grep -q "^platforms $IMG@$D_GOOD$" "$calls" ||
    fail "the platform inspect must use the digest ref, got: $(cat "$calls")"
grep -q "^digest $IMG:$TAG$" "$calls" ||
    fail "the digest inspect should use the tagged ref, got: $(cat "$calls")"

echo "==> an OCI index whose attestation entries are filtered out still passes"
# buildx drops the unknown/unknown attestation manifests inside the template;
# the blank lines a skipping range can leave behind must not become platforms.
FAKE_PLATFORMS="linux/amd64

"
run "$IMG" "$TAG"
[ "$RC" = 0 ] || fail "an amd64-only index should pass; got: $ALL"

echo "==> a multi-arch index fails with the observed, de-duplicated set"
FAKE_PLATFORMS="linux/amd64
linux/arm
linux/arm
linux/arm64
linux/386
linux/ppc64le
linux/riscv64
linux/s390x
"
run "$IMG" "$TAG"
[ "$RC" = 1 ] || fail "a multi-arch index must fail"
case "$ALL" in
*"observed: linux/386 linux/amd64 linux/arm linux/arm64 linux/ppc64le linux/riscv64 linux/s390x"*) : ;;
*) fail "expected the sorted, de-duplicated observed set, got: $ALL" ;;
esac

echo "==> an arm64-only image fails the amd64 assertion"
FAKE_PLATFORMS="linux/arm64
"
run "$IMG" "$TAG"
[ "$RC" = 1 ] || fail "an arm64-only image must fail"
case "$ALL" in
*"observed: linux/arm64"*) : ;;
*) fail "expected 'observed: linux/arm64', got: $ALL" ;;
esac

echo "==> an empty platform set fails rather than passing vacuously"
FAKE_PLATFORMS=""
run "$IMG" "$TAG"
[ "$RC" = 1 ] || fail "an empty platform set must fail"
case "$ALL" in
*"observed: <none>"*) : ;;
*) fail "expected 'observed: <none>', got: $ALL" ;;
esac

echo "==> a malformed digest fails before the platform assertion"
FAKE_PLATFORMS="linux/amd64
"
for bad in "not-a-digest" "sha256:xyz" "sha512:$h8$h8$h8$h8$h8$h8$h8$h8" \
    "sha256:${h8}${h8}${h8}${h8}${h8}${h8}${h8}3333333" ""; do
    FAKE_DIGEST="$bad"
    run "$IMG" "$TAG"
    [ "$RC" = 1 ] || fail "digest '$bad' must be rejected"
    case "$ALL" in
    *"unexpected digest"*) : ;;
    *) fail "expected 'unexpected digest' for '$bad', got: $ALL" ;;
    esac
    if grep -q '^platforms ' "$calls"; then fail "must not inspect platforms after a bad digest"; fi
done

echo "==> an uppercase-hex digest is rejected"
FAKE_DIGEST="sha256:AAAAAAAA$h8$h8$h8$h8$h8$h8$h8"
run "$IMG" "$TAG"
[ "$RC" = 1 ] || fail "an uppercase digest must be rejected"

echo "==> the pin line is appended to GITHUB_STEP_SUMMARY"
FAKE_DIGEST="$D_GOOD"
FAKE_PLATFORMS="linux/amd64
"
sum="$work/summary.md"
: >"$sum"
PATH="$work/bin:$PATH" FAKE_CALLS="$calls" FAKE_DIGEST="$FAKE_DIGEST" \
    FAKE_PLATFORMS="$FAKE_PLATFORMS" GITHUB_STEP_SUMMARY="$sum" \
    "$digest_sh" "$IMG" "$TAG" >/dev/null
[ "$(cat "$sum")" = "- \`$IMG\` → \`$IMG:$TAG@$D_GOOD\`" ] ||
    fail "unexpected step summary line: $(cat "$sum")"

echo "==> the summary is appended to, not truncated (both matrix legs)"
PATH="$work/bin:$PATH" FAKE_CALLS="$calls" FAKE_DIGEST="$FAKE_DIGEST" \
    FAKE_PLATFORMS="$FAKE_PLATFORMS" GITHUB_STEP_SUMMARY="$sum" \
    "$digest_sh" "$IMG-dev" "$TAG" >/dev/null
[ "$(wc -l <"$sum" | tr -d ' ')" = "2" ] || fail "expected two summary lines"

echo "==> an empty GITHUB_STEP_SUMMARY means no summary write"
FAKE_DIGEST="$D_GOOD"
TEST_SUMMARY="" run "$IMG" "$TAG"
[ "$RC" = 0 ] || fail "should pass without GITHUB_STEP_SUMMARY"
[ ! -e "$work/never-written" ] || fail "stray summary file"
TEST_SUMMARY="$work/never-written" run "$IMG" "$TAG"
[ "$RC" = 0 ] && [ -s "$work/never-written" ] || fail "summary should be written when a path is given"

echo "==> bad usage exits 2"
run
[ "$RC" = 2 ] || fail "no args should be a usage error"
run "$IMG"
[ "$RC" = 2 ] || fail "one arg should be a usage error"
run "$IMG" "$TAG" extra
[ "$RC" = 2 ] || fail "three args should be a usage error"
run "" "$TAG"
[ "$RC" = 2 ] || fail "an empty image should be a usage error"
run "$IMG" ""
[ "$RC" = 2 ] || fail "an empty tag should be a usage error"

echo "image digest helper: all cases passed"
