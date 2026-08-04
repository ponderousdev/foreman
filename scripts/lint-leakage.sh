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

rules="
mowing-bidder-web	${self}
ponderous-site	${self}
lawnomator	${self}
[^n]omator	${self}
harmonops	${self}
harmon-infra	${self}
Jessedroptable	${self}|${trust_cfg}
"

fail=0
while IFS="$(printf '\t')" read -r pattern allowed; do
    [ -z "$pattern" ] && continue
    hits="$(git ls-files | grep -Ev '^\.git/' | while IFS= read -r f; do
        [ -f "$f" ] || continue
        if grep -Iq -E "$pattern" "$f" 2>/dev/null; then
            if ! printf '%s\n' "$f" | grep -Eq "$allowed"; then
                printf '%s\n' "$f"
            fi
        fi
    done)"
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
