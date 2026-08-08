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
#          FOREMAN_LOG_FILE FOREMAN_BILLING FOREMAN_READONLY
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

# Credential isolation + auth-method selection. Codex reads OPENAI_API_KEY for
# API-key billing and the ChatGPT login under $CODEX_HOME for subscription
# billing; `forced_login_method` pins which one so a stray key can never
# silently switch billing. Competing provider credentials are stripped so a
# multi-secret unit never hands Codex another vendor's key.
#
# Emits the shared `-c` config overrides into the CODEX_CONFIG array.
CODEX_CONFIG=()
_prepare() {
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
        CODEX_CONFIG+=(-c "forced_login_method=api")
    else
        # Subscription billing uses the ChatGPT login persisted under
        # $CODEX_HOME (auth.json). Drop any API key so it cannot flip billing.
        unset OPENAI_API_KEY
        CODEX_CONFIG+=(-c "forced_login_method=chatgpt")
    fi
    # The provisioning variable never reaches Codex's child process.
    unset FOREMAN_OPENAI_API_KEY

    # Model selection is RUNNER configuration: an operator-set FOREMAN_CODEX_MODEL
    # (or Codex's own config.toml when unset), never an advisory suggest:* label.
    # `-c value` parses as TOML and falls back to a literal string, so a bare
    # model id needs no quoting.
    if [ -n "${FOREMAN_CODEX_MODEL:-}" ]; then
        CODEX_CONFIG+=(-c "model=$FOREMAN_CODEX_MODEL")
    fi

    # Sandbox policy. `codex exec resume` accepts neither --sandbox nor
    # --add-dir, so both the mode and the extra writable root (the result
    # contract lives OUTSIDE the worktree) ride the `-c` overrides that run and
    # resume share.
    if [ "${FOREMAN_READONLY:-0}" = "1" ]; then
        CODEX_CONFIG+=(-c "sandbox_mode=read-only")
    else
        CODEX_CONFIG+=(-c "sandbox_mode=workspace-write")
        CODEX_CONFIG+=(-c "sandbox_workspace_write.writable_roots=[\"$(dirname "$FOREMAN_RESULT_FILE")\"]")
    fi
}

# Stream Codex's JSONL to the log and capture the thread id from the first
# `thread.started` event. pipefail is intentionally OFF: PIPESTATUS[0] carries
# Codex's own exit code out of the pipeline unmasked by the reader block.
_run_stream() {
    set +o pipefail
    codex "${CODEX_CONFIG[@]}" "$@" <"$FOREMAN_PROMPT_FILE" | {
        local session_captured=0 line sid
        while IFS= read -r line; do
            printf '%s\n' "$line" >>"$FOREMAN_LOG_FILE"
            if [ "$session_captured" -eq 0 ]; then
                sid=$(printf '%s' "$line" | sed -n 's/.*"thread_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
                if [ -n "$sid" ]; then
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
        else
            resume_ref="${2:-}"
        fi
        ;;
    *)
        _fail "unknown command: $cmd"
        ;;
    esac

    command -v codex >/dev/null 2>&1 || _fail "codex CLI not found on PATH"

    # attach needs no result/session plumbing — it is interactive triage. Its
    # sandbox override references FOREMAN_RESULT_FILE, so default it harmlessly.
    : "${FOREMAN_RESULT_FILE:=/dev/null}"
    _prepare

    # Manual local triage stays inside the adapter so resumes reuse the same
    # auth, model routing, and sandbox policy as headless runs.
    if [ "$cmd" = "attach" ]; then
        if [ -n "$resume_ref" ]; then
            exec codex "${CODEX_CONFIG[@]}" resume "$resume_ref"
        fi
        exec codex "${CODEX_CONFIG[@]}" resume
    fi

    : "${FOREMAN_PROMPT_FILE:?}" "${FOREMAN_RESULT_FILE:?}" \
        "${FOREMAN_SESSION_FILE:?}" "${FOREMAN_LOG_FILE:?}"

    # Read-only analysis mode (vet): plain-text final output on stdout (foreman
    # captures it), read-only sandbox, no session capture, no resume.
    if [ "${FOREMAN_READONLY:-0}" = "1" ]; then
        exec codex "${CODEX_CONFIG[@]}" exec --skip-git-repo-check - <"$FOREMAN_PROMPT_FILE"
    fi

    # `-` reads the prompt from stdin as the instructions (not an appended
    # <stdin> block). The agent writes the result contract itself; the writable
    # root granted above lets it reach $FOREMAN_RESULT_FILE outside the worktree.
    if [ -n "$resume_ref" ]; then
        _run_stream exec resume --json --skip-git-repo-check "$resume_ref" -
    else
        _run_stream exec --json --skip-git-repo-check -
    fi
}

main "$@"
