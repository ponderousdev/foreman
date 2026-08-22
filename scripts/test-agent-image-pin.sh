#!/usr/bin/env bash
# test-agent-image-pin.sh — unit-test agent-image-pin.sh against a temp-dir
# .foreman.toml fixture: `current` reads the pin, `set` rewrites exactly the
# `image = ` line (leaving the `[verify]` table, every other key, the file mode
# and any trailing comment intact), malformed refs are rejected with a specific
# exit code and message without touching the file, and a repeated `set` is a
# no-op. Run via `task test:image:pin`.
set -euo pipefail
cd "$(dirname "$0")/.."
pin="$PWD/scripts/agent-image-pin.sh"

work=$(mktemp -d)
trap 'chmod -R u+rw "$work" 2>/dev/null || true; rm -rf "$work"' EXIT
f="$work/.foreman.toml"
before="$work/before.toml"

h8="33333333"
D_OLD="$h8$h8$h8$h8$h8$h8$h8$h8" # 64 hex
a8="abcdef01"
D_NEW="$a8$a8$a8$a8$a8$a8$a8$a8"   # 64 hex
SHA40="$a8$a8$a8$a8$a8"            # a 40-hex commit sha
U8="ABCDEF01"                      # uppercase
D_UPPER="$U8$U8$U8$U8$U8$U8$U8$U8" # 64 uppercase hex
D_SHORT=${D_OLD#?}                 # 63 hex
IMG="ghcr.io/ponderousdev/foreman-devcontainer"

fail() {
    echo "TEST FAIL: $*" >&2
    exit 1
}

# fixture [PIN [SUFFIX]] -> (re)write the temp .foreman.toml; omit PIN for a
# file with no image line. SUFFIX is appended after the closing quote.
fixture() {
    {
        echo '# fixture'
        echo 'runner = "local"'
        echo 'backend = "claude"'
        if [ "$#" -ge 1 ]; then echo "image = \"$1\"${2:-}"; fi
        echo 'sandboxed = false'
        echo ''
        echo '[verify]'
        echo 'default = ["task", "verify"]'
    } >"$f"
    cp "$f" "$before"
}

# run ARGS... -> echoes the helper's exit code (output suppressed).
run() {
    _rc=0
    FOREMAN_TOML="$f" "$pin" "$@" >/dev/null 2>&1 || _rc=$?
    echo "$_rc"
}

# expect_fail RC PATTERN ARGS... -> assert the exact exit code AND that the
# combined output contains PATTERN (a bare non-zero exit proves little).
expect_fail() {
    _want="$1"
    _pat="$2"
    shift 2
    _rc=0
    _out=$(FOREMAN_TOML="$f" "$pin" "$@" 2>&1) || _rc=$?
    [ "$_rc" = "$_want" ] || fail "expected exit $_want for '$*', got $_rc"
    case "$_out" in
    *"$_pat"*) : ;;
    *) fail "expected '$_pat' in the output of '$*', got: $_out" ;;
    esac
}

# assert_only_pin_line_changed LABEL -> every non-`image` line must be untouched.
assert_only_pin_line_changed() {
    grep -v '^image = ' "$before" >"$work/a"
    grep -v '^image = ' "$f" >"$work/b"
    diff -u "$work/a" "$work/b" >/dev/null || fail "$1: lines other than the pin changed"
}

# GNU first: on GNU stat `-f` means "filesystem status" and succeeds with the
# wrong output, whereas BSD stat rejects `-c` outright.
mode_of() {
    stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"
}

# accept REF LABEL -> the ref is valid and round-trips through the file.
accept() {
    fixture "$IMG@sha256:$D_OLD"
    [ "$(run set "$1")" = 0 ] || fail "$2 should be accepted"
    [ "$(FOREMAN_TOML="$f" "$pin" current)" = "$1" ] || fail "$2 did not round-trip"
}

# reject REF LABEL -> exit 1 with the grammar message, file byte-identical.
reject() {
    fixture "$IMG@sha256:$D_OLD"
    expect_fail 1 "not a valid image pin" set "$1"
    cmp -s "$before" "$f" || fail "$2 must leave the file untouched"
}

echo "==> current prints the recorded pin"
fixture "$IMG@sha256:$D_OLD"
[ "$(FOREMAN_TOML="$f" "$pin" current)" = "$IMG@sha256:$D_OLD" ] ||
    fail "current should print the seeded pin"

echo "==> set with a digest-only ref rewrites exactly the pin line"
[ "$(run set "$IMG@sha256:$D_NEW")" = 0 ] || fail "digest-only ref should be accepted"
[ "$(FOREMAN_TOML="$f" "$pin" current)" = "$IMG@sha256:$D_NEW" ] || fail "pin not updated"
assert_only_pin_line_changed "digest-only set"

echo "==> set with a tag+digest ref rewrites exactly the pin line"
fixture "$IMG@sha256:$D_OLD"
[ "$(run set "$IMG:sha-$SHA40@sha256:$D_NEW")" = 0 ] || fail "tag+digest ref should be accepted"
[ "$(FOREMAN_TOML="$f" "$pin" current)" = "$IMG:sha-$SHA40@sha256:$D_NEW" ] || fail "pin not updated"
assert_only_pin_line_changed "tag+digest set"

echo "==> a registry with a port, and Docker's -- / __ path separators, are accepted"
accept "localhost:5000/x/y:tag@sha256:$D_NEW" "a registry with a port"
accept "ghcr.io/org/my--image@sha256:$D_NEW" "a doubled-dash path segment"
accept "ghcr.io/org/a__b@sha256:$D_NEW" "a doubled-underscore path segment"

echo "==> a trailing comment on the pin line survives a set"
fixture "$IMG@sha256:$D_OLD" '  # bump via task image:pin:set'
[ "$(FOREMAN_TOML="$f" "$pin" current)" = "$IMG@sha256:$D_OLD" ] ||
    fail "current should ignore a trailing comment"
[ "$(run set "$IMG@sha256:$D_NEW")" = 0 ] || fail "set should pass with a trailing comment"
[ "$(FOREMAN_TOML="$f" "$pin" current)" = "$IMG@sha256:$D_NEW" ] || fail "pin not updated"
grep -q '^image = ".*"  # bump via task image:pin:set$' "$f" ||
    fail "the trailing comment must be preserved"

echo "==> an empty pin value with a quoted trailing comment rewrites cleanly"
fixture "" '  # see "docs"'
[ "$(run set "$IMG@sha256:$D_NEW")" = 0 ] || fail "set should pass on an empty value"
grep -q "^image = \"$IMG@sha256:$D_NEW\"  # see \"docs\"\$" "$f" ||
    fail "empty-value rewrite mangled the line: $(grep '^image = ' "$f")"

echo "==> the file mode is preserved across a set"
fixture "$IMG@sha256:$D_OLD"
chmod 644 "$f"
[ "$(run set "$IMG@sha256:$D_NEW")" = 0 ] || fail "set should pass"
[ "$(mode_of "$f")" = "644" ] || fail "mode changed to $(mode_of "$f"), expected 644"

echo "==> a tag-only ref (no digest) is rejected"
reject "$IMG:latest" "a tag-only ref"

echo "==> a 63-hex digest is rejected"
reject "$IMG@sha256:$D_SHORT" "a 63-hex digest"

echo "==> an uppercase-hex digest is rejected"
reject "$IMG@sha256:$D_UPPER" "an uppercase-hex digest"

echo "==> a sha512 digest is rejected"
reject "$IMG@sha512:$D_OLD$D_OLD" "a sha512 digest"

echo "==> a bare image name with no digest at all is rejected"
reject "$IMG" "a bare image name"

echo "==> a malformed host is rejected"
reject "ghcr.io-/ns/img@sha256:$D_NEW" "a host with a trailing hyphen"
reject "ghcr..io/ns/img@sha256:$D_NEW" "a host with an empty label"

echo "==> a ref containing a newline is rejected before the line-oriented match"
fixture "$IMG@sha256:$D_OLD"
expect_fail 1 "must not contain a newline" set "$(printf 'nope\n%s@sha256:%s' "$IMG" "$D_NEW")"
cmp -s "$before" "$f" || fail "a newline-bearing ref must leave the file untouched"

echo "==> a file with no image line is an error for both subcommands"
fixture
expect_fail 1 "no 'image = " current
expect_fail 1 "no 'image = " set "$IMG@sha256:$D_NEW"
cmp -s "$before" "$f" || fail "a fixture with no image line must be left untouched"

echo "==> a duplicated image line is an error"
fixture "$IMG@sha256:$D_OLD"
echo "image = \"$IMG@sha256:$D_NEW\"" >>"$f"
cp "$f" "$before"
expect_fail 1 "multiple 'image = ' lines" current
expect_fail 1 "multiple 'image = ' lines" set "$IMG@sha256:$D_NEW"
cmp -s "$before" "$f" || fail "a duplicated pin must leave the file untouched"

echo "==> a missing file is an error"
fixture "$IMG@sha256:$D_OLD"
rm -f "$f"
expect_fail 1 "no such file" current

echo "==> an unreadable file is an error"
fixture "$IMG@sha256:$D_OLD"
if [ "$(id -u)" -eq 0 ]; then
    echo "    (skipped: running as root, the mode bits do not apply)"
else
    chmod 000 "$f"
    expect_fail 1 "not readable" current
    chmod 644 "$f"
fi

echo "==> a repeated set with the same ref is an idempotent no-op"
fixture "$IMG@sha256:$D_OLD"
[ "$(run set "$IMG@sha256:$D_NEW")" = 0 ] || fail "first set should pass"
cp "$f" "$before"
[ "$(run set "$IMG@sha256:$D_NEW")" = 0 ] || fail "second set should pass"
cmp -s "$before" "$f" || fail "an idempotent set must not rewrite the file"

echo "==> bad usage exits 2"
fixture "$IMG@sha256:$D_OLD"
expect_fail 2 "usage:"
expect_fail 2 "usage:" bogus
expect_fail 2 "usage:" set
expect_fail 2 "usage:" current extra

echo "image pin helper: all cases passed"
