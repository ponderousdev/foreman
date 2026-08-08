#!/usr/bin/env bash
# codex-cli.sh — Foreman backend adapter for OpenAI's Codex CLI (`codex exec`).
#
# Codex is a different harness from Claude Code, so this adapter is standalone
# (it does NOT source lib/claude-common.sh). It satisfies the same contract
# (see src/foreman/backend.py):
#   codex-cli.sh run                  dispatch with $FOREMAN_PROMPT_FILE
#   codex-cli.sh resume <session-id>  resume a prior session with a new prompt
#   codex-cli.sh attach [session-id]  interactive local triage
#   codex-cli.sh capabilities         print capability tokens
#
# Env in:  FOREMAN_PROMPT_FILE FOREMAN_RESULT_FILE FOREMAN_SESSION_FILE
#          FOREMAN_LOG_FILE FOREMAN_BILLING FOREMAN_READONLY FOREMAN_MAX_TURNS
#          FOREMAN_OPENAI_API_KEY (billing=api) FOREMAN_CODEX_MODEL (optional)
# Out:     SESSION_REF=<thread-id> appended to $FOREMAN_SESSION_FILE from the
#          FIRST `thread.started` event (killed agents emit no final event, and
#          resuming a dead agent is exactly when the ref matters).
#
# Codex exec's JSONL stream reports token usage but no verified USD cost, so
# this adapter deliberately does not advertise or persist `cost`.
#
# Timeouts are enforced by foreman (backend.py), not here.
set -euo pipefail

ADAPTER_NAME="codex-cli.sh"
ADAPTER_CAPABILITIES="resume attach"

_fail() {
    echo "${ADAPTER_NAME}: $*" >&2
    exit 1
}

# Codex thread ids are UUIDs (8-4-4-4-12 hex). Session refs flow through the
# agent-writable session file, so a corrupted ledger could smuggle an
# option-shaped value (e.g. `--last`, which Codex resolves to the most recent
# GLOBAL thread) into Codex's positional parser and hijack another unit's
# session. Only ever capture or pass canonical UUIDs.
_is_uuid() {
    local re='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
    [[ "$1" =~ $re ]]
}

# Serialize $1 as a TOML basic string so a path containing a quote, backslash,
# or a control character cannot break (or alter) the `writable_roots` array
# override. TOML basic strings forbid raw control characters; escape the ones
# with shorthands and refuse the rest rather than emit an invalid override.
_toml_str() {
    local s="$1"
    s="${s//\\/\\\\}" # backslash first
    s="${s//\"/\\\"}" # then double-quote
    s="${s//$'\b'/\\b}"
    s="${s//$'\t'/\\t}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\f'/\\f}"
    s="${s//$'\r'/\\r}"
    case "$s" in
    *[$'\001'-$'\037']*) _fail "path contains an unsupported control character" ;;
    esac
    printf '"%s"' "$s"
}

# Credential isolation + auth-method selection into AUTH_CONFIG. Codex reads
# OPENAI_API_KEY for API-key billing and the ChatGPT login under $CODEX_HOME for
# subscription billing; `forced_login_method` pins which one so a stray key can
# never silently switch billing. Competing provider credentials are stripped so
# a multi-secret unit never hands Codex another vendor's key.
AUTH_CONFIG=()
_prepare_auth() {
    unset FOREMAN_ANTHROPIC_API_KEY ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
    unset FOREMAN_DEEPSEEK_API_KEY DEEPSEEK_API_KEY
    unset FOREMAN_KIMI_API_KEY MOONSHOT_API_KEY KIMI_API_KEY
    unset FOREMAN_GLM_API_KEY ZAI_API_KEY ZHIPUAI_API_KEY GLM_API_KEY

    local api_key="${FOREMAN_OPENAI_API_KEY:-}"
    if [ "${FOREMAN_BILLING:-subscription}" = "api" ]; then
        if [ -z "$api_key" ]; then
            # Fail closed WITHOUT echoing any secret material.
            _fail "billing=api needs FOREMAN_OPENAI_API_KEY"
        fi
        export OPENAI_API_KEY="$api_key"
        AUTH_CONFIG=(-c "forced_login_method=api")
    else
        # Subscription billing uses the ChatGPT login persisted under
        # $CODEX_HOME (auth.json). Drop any API key so it cannot flip billing.
        unset OPENAI_API_KEY
        AUTH_CONFIG=(-c "forced_login_method=chatgpt")
    fi
    # The provisioning variable never reaches Codex's child process.
    unset FOREMAN_OPENAI_API_KEY

    # Model selection is RUNNER configuration: an operator-set FOREMAN_CODEX_MODEL
    # (or Codex's own config.toml when unset), never an advisory suggest:* label.
    # `-c value` parses as TOML and falls back to a literal string, so a bare
    # model id needs no quoting.
    if [ -n "${FOREMAN_CODEX_MODEL:-}" ]; then
        AUTH_CONFIG+=(-c "model=$FOREMAN_CODEX_MODEL")
    fi
}

# Sandbox policy for headless run/resume into SANDBOX_CONFIG. `codex exec resume`
# accepts neither --sandbox nor --add-dir, so the mode and every extra writable
# root ride the `-c` overrides that run and resume share.
#
# The workspace root (the worktree) is writable by default; two extra roots the
# worktree model needs are added explicitly:
#   - the result-contract dir, which lives OUTSIDE the worktree, and
#   - the worktree's real Git metadata dir. Foreman's linked worktrees keep
#     `.git/worktrees/<wt>` and the shared object store outside the worktree, so
#     under workspace-write (which keeps `.git` read-only) every `git commit`
#     fails with a read-only filesystem error without it.
# network_access mirrors the runner's intent (the read-only GH token, dependency
# fetches, test services); the runner — not Codex's internal sandbox — is
# Foreman's isolation boundary, exactly as for the `claude` adapter.
SANDBOX_CONFIG=()
_build_sandbox_config() {
    if [ "${FOREMAN_READONLY:-0}" = "1" ]; then
        SANDBOX_CONFIG=(-c "sandbox_mode=read-only")
        return
    fi
    local roots=() common joined
    roots+=("$(_toml_str "$(dirname "$FOREMAN_RESULT_FILE")")")
    common=$(git rev-parse --git-common-dir 2>/dev/null || true)
    if [ -n "$common" ]; then
        # git may report the common dir relative to cwd; resolve to absolute.
        common=$(cd "$common" 2>/dev/null && pwd) || common=""
    fi
    [ -n "$common" ] && roots+=("$(_toml_str "$common")")
    joined=$(
        IFS=,
        echo "${roots[*]}"
    )
    SANDBOX_CONFIG=(
        -c "sandbox_mode=workspace-write"
        -c "sandbox_workspace_write.network_access=true"
        -c "sandbox_workspace_write.writable_roots=[$joined]"
    )
}

# Stream Codex's JSONL to the log and capture the thread id from the first
# `thread.started` event (validated as a UUID before it is persisted). pipefail
# is intentionally OFF: PIPESTATUS[0] carries Codex's own exit code out of the
# pipeline unmasked by the reader block.
_run_stream() {
    set +o pipefail
    codex "${AUTH_CONFIG[@]}" "${SANDBOX_CONFIG[@]}" "$@" <"$FOREMAN_PROMPT_FILE" | {
        local session_captured=0 line sid
        while IFS= read -r line; do
            printf '%s\n' "$line" >>"$FOREMAN_LOG_FILE"
            if [ "$session_captured" -eq 0 ]; then
                sid=$(printf '%s' "$line" | sed -n 's/.*"thread_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
                if [ -n "$sid" ] && _is_uuid "$sid"; then
                    echo "SESSION_REF=$sid" >>"$FOREMAN_SESSION_FILE"
                    session_captured=1
                fi
            fi
        done
    }
    exit "${PIPESTATUS[0]}"
}

main() {
    local cmd="${1:-run}"
    local resume_ref=""
    case "$cmd" in
    capabilities)
        echo "$ADAPTER_CAPABILITIES"
        exit 0
        ;;
    run) ;;
    resume)
        resume_ref="${2:-}"
        [ -n "$resume_ref" ] || _fail "resume requires a session ref"
        # The ref comes from the agent-writable session ledger — reject
        # anything that is not a canonical UUID (see _is_uuid).
        _is_uuid "$resume_ref" || _fail "resume ref is not a valid session id"
        ;;
    attach)
        if [ "${2:-}" = "--session-file" ]; then
            local attach_session_file="${3:-}"
            [ -n "$attach_session_file" ] || _fail "attach --session-file requires a path"
            [ -r "$attach_session_file" ] || _fail "attach session file is not readable"
            local attach_line
            while IFS= read -r attach_line; do
                case "$attach_line" in
                SESSION_REF=*) resume_ref="${attach_line#SESSION_REF=}" ;;
                esac
            done <"$attach_session_file"
            [ -n "$resume_ref" ] || _fail "attach session file has no session ref"
            # Ledger-sourced ref: same UUID guard as resume.
            _is_uuid "$resume_ref" || _fail "attach session ref is not a valid session id"
        else
            resume_ref="${2:-}"
        fi
        ;;
    *)
        _fail "unknown command: $cmd"
        ;;
    esac

    command -v codex >/dev/null 2>&1 || _fail "codex CLI not found on PATH"
    _prepare_auth

    # Manual local triage stays inside the adapter so resumes reuse the same
    # auth and model routing as headless runs. attach is interactive (the human
    # drives approvals), so it takes no headless sandbox overrides — that also
    # avoids granting an extra writable root for a result path it never uses.
    if [ "$cmd" = "attach" ]; then
        if [ -n "$resume_ref" ]; then
            exec codex "${AUTH_CONFIG[@]}" resume "$resume_ref"
        fi
        # No ref (e.g. the unit died before emitting one): start a FRESH
        # interactive session. Bare `codex resume` would open the global session
        # picker and could select an unrelated unit's conversation.
        exec codex "${AUTH_CONFIG[@]}"
    fi

    : "${FOREMAN_PROMPT_FILE:?}" "${FOREMAN_RESULT_FILE:?}" \
        "${FOREMAN_SESSION_FILE:?}" "${FOREMAN_LOG_FILE:?}"

    # Codex exec has no per-turn cap. Rather than silently ignore a configured
    # bound (this adapter also reports no cost, so timeout would be the only
    # remaining bound), fail closed when a nonzero max_turns is requested.
    case "${FOREMAN_MAX_TURNS:-0}" in
    0 | '') ;;
    *) _fail "codex-cli cannot enforce max_turns=${FOREMAN_MAX_TURNS}; set max_turns=0 and bound the unit via timeout" ;;
    esac

    _build_sandbox_config

    # Read-only analysis mode (vet): plain-text final output on stdout (foreman
    # captures it), read-only sandbox, no session capture, no resume.
    if [ "${FOREMAN_READONLY:-0}" = "1" ]; then
        exec codex "${AUTH_CONFIG[@]}" "${SANDBOX_CONFIG[@]}" exec --skip-git-repo-check - <"$FOREMAN_PROMPT_FILE"
    fi

    # `-` reads the prompt from stdin as the instructions (not an appended
    # <stdin> block). The agent writes the result contract itself; the writable
    # roots granted above let it reach $FOREMAN_RESULT_FILE and commit in the
    # worktree.
    if [ -n "$resume_ref" ]; then
        _run_stream exec resume --json --skip-git-repo-check "$resume_ref" -
    else
        _run_stream exec --json --skip-git-repo-check -
    fi
}

main "$@"
