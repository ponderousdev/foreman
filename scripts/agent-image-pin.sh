#!/usr/bin/env bash
# agent-image-pin.sh — read/write the agent image digest pin in .foreman.toml.
#
# The pin is `image = "<registry>/<repo>[:<tag>]@sha256:<64 hex>"`: registry-
# agnostic, digest REQUIRED, tag optional. GHCR tags are mutable, so the digest
# is the identity. The bump path is deliberately manual (the publishing
# workflow holds no contents:write): open the `publish (ai)` job's step summary
# and copy the ref, or re-resolve it locally with `task image:digest`, then run
# this. `set` reads the pin back after writing and fails unless it round-trips.
# Set FOREMAN_TOML to point at another file (used by the tests).
# Run via `task image:pin:current` / `task image:pin:set REF=…`.
set -euo pipefail

cd "$(dirname "$0")/.."
file="${FOREMAN_TOML:-$PWD/.foreman.toml}"

# POSIX ERE twin of IMAGE_PIN_RE in src/foreman/config.py — keep the two in
# sync (tests/test_config.py::test_shell_pin_regex_agrees_with_python parses
# this very line and asserts the two agree over a shared corpus).
pin_re='^([A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*(:[0-9]{1,5})?/)?[a-z0-9]+(([._]|__|-+)[a-z0-9]+)*(/[a-z0-9]+(([._]|__|-+)[a-z0-9]+)*)*(:[A-Za-z0-9_][A-Za-z0-9._-]{0,127})?@sha256:[0-9a-f]{64}$'

usage() {
    cat >&2 <<'USAGE'
usage: agent-image-pin.sh current
       agent-image-pin.sh set <image>[:<tag>]@sha256:<64 hex>
USAGE
    exit 2
}

die() {
    echo "error: $*" >&2
    exit 1
}

# Fail unless the file is readable and holds exactly one `image = "…"` line.
require_single_pin_line() {
    [ -f "$file" ] || die "no such file: $file"
    [ -r "$file" ] || die "not readable: $file"
    _rc=0
    _count=$(grep -c '^image = "' "$file") || _rc=$?
    # grep exits 1 (with a "0" count) for no match; anything above that is a
    # real read failure and must not be mistaken for an absent pin line.
    if [ "$_rc" -eq 1 ]; then
        _count=0
    elif [ "$_rc" -ne 0 ]; then
        die "cannot read $file"
    fi
    [ "$_count" -ne 0 ] || die "no 'image = \"…\"' line in $file"
    [ "$_count" -eq 1 ] || die "multiple 'image = ' lines in $file ($_count)"
}

# The pin value: everything between the first pair of quotes, so a trailing
# comment on the line is tolerated.
read_pin() {
    sed -n 's/^image = "\([^"]*\)".*$/\1/p' "$file"
}

cmd_current() {
    require_single_pin_line
    _pin=$(read_pin)
    [ -n "$_pin" ] || die "could not parse the image pin line in $file"
    echo "$_pin"
}

cmd_set() {
    ref="$1"
    # grep -E is line-oriented, so a ref containing a newline could smuggle an
    # invalid first line past an anchored match. Reject it before validating.
    if [ "$(printf '%s' "$ref" | wc -l | tr -d ' ')" != "0" ]; then
        die "the image pin must not contain a newline"
    fi
    if ! printf '%s\n' "$ref" | grep -Eq "$pin_re"; then
        echo "error: not a valid image pin: $ref" >&2
        echo "       expected <registry>/<repo>[:<tag>]@sha256:<64 lowercase hex>" >&2
        echo "       — pin by digest; a tag alone is mutable and is not accepted" >&2
        exit 1
    fi
    require_single_pin_line

    if [ "$(read_pin)" = "$ref" ]; then
        echo "image pin already $ref"
        return 0
    fi

    # Rewrite the single line via a temp file: no `sed -i` portability games,
    # and awk avoids sed's delimiter hazard (refs contain `/`). Anything after
    # the closing quote (a trailing comment) is carried across untouched.
    tmp=$(mktemp)
    # shellcheck disable=SC2064  # expand $tmp now, at trap-install time
    trap "rm -f '$tmp'" EXIT
    awk -v ref="$ref" '
        index($0, "image = \"") == 1 {
            rest = substr($0, 10)
            q = index(rest, "\"")
            print "image = \"" ref "\"" (q ? substr(rest, q + 1) : "")
            next
        }
        { print }
    ' "$file" >"$tmp"
    # Write THROUGH the existing file rather than mv'ing over it: mv would
    # replace the inode and stamp mktemp's 0600 mode onto .foreman.toml (and
    # would clobber a symlink rather than follow it).
    cat "$tmp" >"$file"
    rm -f "$tmp"
    trap - EXIT

    # Round-trip guard: what we just wrote must parse back to exactly the ref.
    _wrote=$(read_pin)
    [ "$_wrote" = "$ref" ] || die "wrote the pin but read back '$_wrote' from $file"

    echo "image pin set to $ref"
}

[ "$#" -ge 1 ] || usage
case "$1" in
current)
    [ "$#" -eq 1 ] || usage
    cmd_current
    ;;
set)
    [ "$#" -eq 2 ] || usage
    cmd_set "$2"
    ;;
*) usage ;;
esac
