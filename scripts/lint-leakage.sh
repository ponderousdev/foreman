#!/usr/bin/env bash
# lint-leakage.sh — deny personal-identity and private-infrastructure
# identifiers in shipped content (#34, enforced permanently after the
# pre-publication sweep). The repo is public: everything git ships is
# world-readable, so names of other private repos, private orgs, and
# non-consenting individuals must never (re)enter the tree.
#
# Rules are `pattern<TAB>allowed-file-regex` — a hit in a file matching the
# allowed regex is a deliberate, documented exception; anywhere else it
# fails. '-' means no exceptions. This file and its Taskfile wiring are
# self-allowlisted (the patterns must be written down somewhere).
#
# Portable: macOS bash 3.2 (no mapfile), POSIX grep -E.
set -euo pipefail

cd "$(dirname "$0")/.."

self='^scripts/lint-leakage\.sh$'
# Live D13 trust config must name real accounts; the disclosure there is a
# deliberate maintainer decision (see #34). Everything else is denied.
trust_cfg='^(\.foreman\.toml|tests/test_config\.py)$'

# '-' in the allowed column = no exceptions anywhere.
rules="
mowing-bidder-web	${self}
ponderous-(site|infra|hub|docs)	${self}
lawnomator	${self}
(^|[^[:alnum:]])omator	${self}
harmonops	${self}
harmon-infra	${self}
Jessedroptable	${self}|${trust_cfg}
"

fail=0
while IFS="$(printf '\t')" read -r pattern allowed; do
    [ -z "$pattern" ] && continue
    # '-' = the documented no-exceptions sentinel, not a path regex.
    [ "$allowed" = "-" ] && allowed='^$'
    hits="$(git ls-files | grep -Ev '^\.git/' | while IFS= read -r f; do
        # The pathname itself is shipped content (a denied name can leak as a
        # filename), and so is a symlink's stored target — neither is covered
        # by a content grep.
        if printf '%s\n' "$f" | grep -Eiq "$pattern"; then
            if ! printf '%s\n' "$f" | grep -Eq "$allowed"; then
                printf '%s (pathname)\n' "$f"
                continue
            fi
        fi
        if [ -L "$f" ]; then
            if readlink "$f" | grep -Eiq "$pattern" &&
                ! printf '%s\n' "$f" | grep -Eq "$allowed"; then
                printf '%s (symlink target)\n' "$f"
            fi
            continue
        fi
        [ -f "$f" ] || continue
        if grep -aiq -E "$pattern" "$f" 2>/dev/null; then
            if ! printf '%s\n' "$f" | grep -Eq "$allowed"; then
                printf '%s\n' "$f"
            fi
        fi
    done)"
    # The worktree loop above lints what you are editing; this lints what a
    # push of the CURRENT branch actually publishes (committed state can
    # differ from the worktree). Pushing a ref other than the checked-out
    # HEAD is not covered locally — CI runs this same guard on every PR head.
    head_hits="$({
        git grep -ail -E "$pattern" HEAD -- . 2>/dev/null | sed 's/^HEAD://'
        git ls-tree -r --name-only HEAD | grep -Ei "$pattern" || true
        # git grep skips symlink blobs, and --name-only sees only their
        # paths — a committed link TARGET is published content too.
        git ls-tree -r HEAD | while IFS= read -r entry; do
            case "$entry" in
            120000*)
                blob="$(printf '%s' "$entry" | awk '{print $3}')"
                path="$(printf '%s' "$entry" | cut -f2)"
                if git cat-file blob "$blob" 2>/dev/null | grep -aiq -E "$pattern"; then
                    printf '%s\n' "$path"
                fi
                ;;
            esac
        done
    } | sort -u | while IFS= read -r f; do
        if ! printf '%s\n' "$f" | grep -Eq "$allowed"; then
            printf '%s (committed on HEAD)\n' "$f"
        fi
    done)"
    if [ -n "$head_hits" ]; then
        hits="$(printf '%s\n%s' "$hits" "$head_hits" | sed '/^$/d' | sort -u)"
    fi
    if [ -n "$hits" ]; then
        echo "LEAKAGE: pattern '$pattern' found in shipped content:" >&2
        printf '%s\n' "$hits" | sed 's/^/  /' >&2
        fail=1
    fi
done <<EOF
$rules
EOF

if [ "$fail" -ne 0 ]; then
    echo "lint-leakage: private-identity/infrastructure identifiers must not ship (see scripts/lint-leakage.sh, #34)" >&2
    exit 1
fi
echo "lint-leakage: clean"
