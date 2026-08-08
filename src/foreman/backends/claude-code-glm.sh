#!/usr/bin/env bash
# claude-code-glm.sh — Claude Code against Z.ai's GLM Anthropic-compatible endpoint.
#
# The adapter hardwires the GLM family. Foreman passes the dedicated
# FOREMAN_GLM_API_KEY; this process converts it to the provider's
# Anthropic-compatible environment and removes competing credentials before
# Claude Code starts. Timeouts remain enforced by backend.py.
set -euo pipefail

FOREMAN_ADAPTER_NAME="claude-code-glm.sh"
# Claude Code sessions can resume against the same provider configuration.
# Its custom-provider stream does not provide a cost value Foreman can verify,
# so this adapter deliberately does not advertise or persist `cost`.
FOREMAN_ADAPTER_CAPABILITIES="resume attach"

foreman_claude_prepare() {
    if [ "${FOREMAN_BILLING:-subscription}" != "api" ]; then
        _foreman_claude_fail "GLM requires billing=api"
        return 1
    fi
    local api_key="${FOREMAN_GLM_API_KEY:-}"
    if [ -z "$api_key" ]; then
        _foreman_claude_fail "billing=api needs FOREMAN_GLM_API_KEY"
        return 1
    fi

    # Keep the provisioning variable and every competing auth route out of the
    # child environment. Only the provider-compatible token survives.
    unset FOREMAN_GLM_API_KEY FOREMAN_KIMI_API_KEY FOREMAN_DEEPSEEK_API_KEY FOREMAN_ANTHROPIC_API_KEY
    unset ZAI_API_KEY ZHIPUAI_API_KEY GLM_API_KEY MOONSHOT_API_KEY KIMI_API_KEY DEEPSEEK_API_KEY
    unset ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN
    unset FOREMAN_OPENAI_API_KEY OPENAI_API_KEY
    export ANTHROPIC_BASE_URL="https://api.z.ai/api/anthropic"
    export ANTHROPIC_AUTH_TOKEN="$api_key"
    export ANTHROPIC_MODEL="glm-5.2[1m]"
    export ANTHROPIC_DEFAULT_OPUS_MODEL="glm-5.2[1m]"
    export ANTHROPIC_DEFAULT_SONNET_MODEL="glm-5.2[1m]"
    export ANTHROPIC_DEFAULT_HAIKU_MODEL="glm-4.7"
    export ANTHROPIC_DEFAULT_FABLE_MODEL="glm-5.2[1m]"
    export CLAUDE_CODE_SUBAGENT_MODEL="glm-4.7"
    # GLM responses can run long. Keep the Anthropic client's per-request
    # timeout above foreman's whole-unit budget (FOREMAN_TIMEOUT_MIN, enforced
    # in backend.py) so the supervisor always owns the deadline and the client
    # never aborts a request first. Fall back to a generous fixed value when the
    # unit runs uncapped (timeout_min = 0) or the value is not a number.
    _glm_timeout_min="${FOREMAN_TIMEOUT_MIN:-0}"
    case "$_glm_timeout_min" in
    '' | *[!0-9]*) _glm_timeout_min=0 ;;
    esac
    if [ "$_glm_timeout_min" -gt 0 ]; then
        export API_TIMEOUT_MS="$(((_glm_timeout_min + 10) * 60 * 1000))"
    else
        export API_TIMEOUT_MS="3000000"
    fi
    export CLAUDE_CODE_EFFORT_LEVEL="max"
    export ENABLE_CLAUDEAI_MCP_SERVERS="false"
}

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# shellcheck source=lib/claude-common.sh
. "$script_dir/lib/claude-common.sh"
foreman_claude_main "$@"
