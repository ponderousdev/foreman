#!/usr/bin/env bash
# claude-code-kimi.sh — Claude Code against Kimi's Anthropic-compatible endpoint.
#
# The adapter hardwires the Kimi family. Foreman passes the dedicated
# FOREMAN_KIMI_API_KEY; this process converts it to the provider's
# Anthropic-compatible environment and removes competing credentials before
# Claude Code starts. Timeouts remain enforced by backend.py.
set -euo pipefail

FOREMAN_ADAPTER_NAME="claude-code-kimi.sh"
# Claude Code sessions can resume against the same provider configuration.
# Its custom-provider stream does not provide a cost value Foreman can verify,
# so this adapter deliberately does not advertise or persist `cost`.
FOREMAN_ADAPTER_CAPABILITIES="resume attach"

foreman_claude_prepare() {
    if [ "${FOREMAN_BILLING:-subscription}" != "api" ]; then
        _foreman_claude_fail "Kimi requires billing=api"
        return 1
    fi
    local api_key="${FOREMAN_KIMI_API_KEY:-}"
    if [ -z "$api_key" ]; then
        _foreman_claude_fail "billing=api needs FOREMAN_KIMI_API_KEY"
        return 1
    fi

    # Keep the provisioning variable and every competing auth route out of the
    # child environment. Only the provider-compatible token survives.
    unset FOREMAN_KIMI_API_KEY FOREMAN_DEEPSEEK_API_KEY FOREMAN_ANTHROPIC_API_KEY
    unset MOONSHOT_API_KEY KIMI_API_KEY DEEPSEEK_API_KEY ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN
    export ANTHROPIC_BASE_URL="https://api.moonshot.ai/v1"
    export ANTHROPIC_AUTH_TOKEN="$api_key"
    export ANTHROPIC_MODEL="kimi-k3"
    export ANTHROPIC_DEFAULT_OPUS_MODEL="kimi-k3"
    export ANTHROPIC_DEFAULT_SONNET_MODEL="kimi-k3"
    export ANTHROPIC_DEFAULT_HAIKU_MODEL="kimi-k2.7-code-highspeed"
    export ANTHROPIC_DEFAULT_FABLE_MODEL="kimi-k3"
    export CLAUDE_CODE_SUBAGENT_MODEL="kimi-k2.7-code-highspeed"
    export CLAUDE_CODE_EFFORT_LEVEL="max"
    export ENABLE_CLAUDEAI_MCP_SERVERS="false"
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=lib/claude-common.sh
. "$script_dir/lib/claude-common.sh"
foreman_claude_main "$@"
