#!/usr/bin/env bash
# claude-code-deepseek.sh — Claude Code against DeepSeek's Anthropic endpoint.
#
# The adapter hardwires the DeepSeek family. Foreman passes the dedicated
# FOREMAN_DEEPSEEK_API_KEY; this process converts it to the provider's
# Anthropic-compatible environment and removes competing credentials before
# Claude Code starts. Timeouts remain enforced by backend.py.
set -euo pipefail

FOREMAN_ADAPTER_NAME="claude-code-deepseek.sh"
# Claude Code sessions can resume against the same provider configuration.
# Its custom-provider stream does not provide a cost value Foreman can verify,
# so this adapter deliberately does not advertise or persist `cost`.
FOREMAN_ADAPTER_CAPABILITIES="resume attach"

foreman_claude_prepare() {
    if [ "${FOREMAN_BILLING:-subscription}" != "api" ]; then
        _foreman_claude_fail "DeepSeek requires billing=api"
        return 1
    fi
    local api_key="${FOREMAN_DEEPSEEK_API_KEY:-}"
    if [ -z "$api_key" ]; then
        _foreman_claude_fail "billing=api needs FOREMAN_DEEPSEEK_API_KEY"
        return 1
    fi

    # Keep the provisioning variable and every competing auth route out of the
    # child environment. Only the provider-compatible token survives.
    unset FOREMAN_DEEPSEEK_API_KEY FOREMAN_ANTHROPIC_API_KEY
    unset DEEPSEEK_API_KEY ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN
    export ANTHROPIC_BASE_URL="https://api.deepseek.com/anthropic"
    export ANTHROPIC_AUTH_TOKEN="$api_key"
    export ANTHROPIC_MODEL="deepseek-v4-pro[1m]"
    export ANTHROPIC_DEFAULT_OPUS_MODEL="deepseek-v4-pro[1m]"
    export ANTHROPIC_DEFAULT_SONNET_MODEL="deepseek-v4-pro[1m]"
    export ANTHROPIC_DEFAULT_HAIKU_MODEL="deepseek-v4-flash"
    export ANTHROPIC_DEFAULT_FABLE_MODEL="deepseek-v4-pro[1m]"
    export CLAUDE_CODE_SUBAGENT_MODEL="deepseek-v4-flash"
    export CLAUDE_CODE_EFFORT_LEVEL="max"
    export ENABLE_CLAUDEAI_MCP_SERVERS="false"
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=lib/claude-common.sh
. "$script_dir/lib/claude-common.sh"
foreman_claude_main "$@"
