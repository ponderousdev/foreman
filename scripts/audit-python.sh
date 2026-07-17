#!/usr/bin/env bash
# Dependency audit for the uv-managed Python package: export the locked
# dependency set (all groups, without the project itself) and run pip-audit
# against the exact pins. Skips cleanly when there is no lockfile yet.
set -euo pipefail

# renovate: datasource=pypi depName=pip-audit
PIP_AUDIT_VERSION=2.10.1

if [ ! -f uv.lock ]; then
    echo "audit-python: no uv.lock — nothing to audit."
    exit 0
fi
if ! command -v uv >/dev/null 2>&1; then
    echo "audit-python: uv not installed — cannot audit (install: brew install uv)" >&2
    exit 1
fi

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
uv export --frozen --all-groups --format requirements-txt --no-emit-project --no-hashes --quiet >"$tmp"
uvx --from "pip-audit==${PIP_AUDIT_VERSION}" pip-audit --no-deps --disable-pip -r "$tmp"
