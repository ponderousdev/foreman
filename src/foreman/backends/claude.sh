#!/usr/bin/env bash
# claude.sh — Foreman backend adapter for Claude Code (headless print mode).
#
# Contract (see src/foreman/backend.py):
#   claude.sh run                  dispatch with $FOREMAN_PROMPT_FILE
#   claude.sh resume <session-id>  resume a prior session with a new prompt
#   claude.sh attach [session-id]  interactive local triage
#   claude.sh capabilities         print capability tokens
#
# Env in:  FOREMAN_PROMPT_FILE FOREMAN_RESULT_FILE FOREMAN_SESSION_FILE
#          FOREMAN_LOG_FILE FOREMAN_PERMISSION_MODE FOREMAN_BILLING
#          FOREMAN_MAX_TURNS FOREMAN_READONLY FOREMAN_ANTHROPIC_API_KEY
# Out:     SESSION_REF=<id> appended to $FOREMAN_SESSION_FILE from the FIRST
#          stream event (killed agents emit no final event, and resuming dead
#          agents is exactly when the ref matters), COST_USD=<x> when known.
#
# Timeouts are enforced by foreman (backend.py), not here.
set -euo pipefail

FOREMAN_ADAPTER_NAME="claude.sh"
FOREMAN_ADAPTER_CAPABILITIES="resume cost attach"

# Billing isolation (spec A11): in api mode the key is exported ONLY into this
# adapter process. The container-wide ANTHROPIC_API_KEY strip (init-env.sh,
# shell-aliases.sh) stays intact for interactive sessions.
foreman_claude_prepare() {
    # A unit may receive more than one explicitly provisioned adapter secret;
    # keep unrelated provider credentials out of Claude Code's child process.
    unset FOREMAN_DEEPSEEK_API_KEY DEEPSEEK_API_KEY
    if [ "${FOREMAN_BILLING:-subscription}" = "api" ]; then
        [ -n "${FOREMAN_ANTHROPIC_API_KEY:-}" ] || {
            _foreman_claude_fail "billing=api needs FOREMAN_ANTHROPIC_API_KEY"
            return 1
        }
        export ANTHROPIC_API_KEY="$FOREMAN_ANTHROPIC_API_KEY"
        unset CLAUDE_CODE_OAUTH_TOKEN
    fi
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=lib/claude-common.sh
. "$script_dir/lib/claude-common.sh"
foreman_claude_main "$@"
