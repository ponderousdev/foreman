#!/usr/bin/env bash
# shellcheck shell=bash
#
# Shared Claude Code execution contract. Provider adapters source this file,
# define foreman_claude_prepare(), then call foreman_claude_main "$@". Provider
# credentials and model routing stay in the adapter; stream/session/result
# mechanics stay identical across every Claude Code harness.

_foreman_claude_fail() {
    echo "${FOREMAN_ADAPTER_NAME}: $*" >&2
    return 1
}

foreman_claude_main() {
    local cmd="${1:-run}"
    local resume_ref=""
    case "$cmd" in
    capabilities)
        echo "$FOREMAN_ADAPTER_CAPABILITIES"
        return 0
        ;;
    run) ;;
    resume)
        resume_ref="${2:-}"
        [ -n "$resume_ref" ] || {
            _foreman_claude_fail "resume requires a session ref"
            return 1
        }
        ;;
    attach)
        if [ "${2:-}" = "--session-file" ]; then
            local attach_session_file="${3:-}"
            [ -n "$attach_session_file" ] || {
                _foreman_claude_fail "attach --session-file requires a path"
                return 1
            }
            [ -r "$attach_session_file" ] || {
                _foreman_claude_fail "attach session file is not readable"
                return 1
            }
            local attach_line
            while IFS= read -r attach_line; do
                case "$attach_line" in
                SESSION_REF=*) resume_ref="${attach_line#SESSION_REF=}" ;;
                esac
            done <"$attach_session_file"
            [ -n "$resume_ref" ] || {
                _foreman_claude_fail "attach session file has no session ref"
                return 1
            }
        else
            resume_ref="${2:-}"
        fi
        ;;
    *)
        _foreman_claude_fail "unknown command: $cmd"
        return 1
        ;;
    esac

    command -v claude >/dev/null 2>&1 || {
        _foreman_claude_fail "claude CLI not found on PATH"
        return 1
    }
    foreman_claude_prepare || return

    # Manual local triage stays inside the provider adapter so resumes use the
    # same endpoint, model routing, and credential isolation as headless runs.
    if [ "$cmd" = "attach" ]; then
        if [ -n "$resume_ref" ]; then
            exec claude --resume "$resume_ref"
        fi
        exec claude
    fi

    : "${FOREMAN_PROMPT_FILE:?}" "${FOREMAN_RESULT_FILE:?}" \
        "${FOREMAN_SESSION_FILE:?}" "${FOREMAN_LOG_FILE:?}"

    # Read-only analysis mode (vet): plain-text final output on stdout
    # (foreman captures it), no file edits, no shell.
    if [ "${FOREMAN_READONLY:-0}" = "1" ]; then
        exec claude -p --permission-mode default \
            --disallowedTools Edit Write NotebookEdit Bash \
            <"$FOREMAN_PROMPT_FILE"
    fi

    local args=(-p --output-format stream-json --verbose)
    args+=(--permission-mode "${FOREMAN_PERMISSION_MODE:-acceptEdits}")
    # The result contract lives OUTSIDE the worktree; grant only that directory.
    args+=(--add-dir "$(dirname "$FOREMAN_RESULT_FILE")")
    if [ "${FOREMAN_MAX_TURNS:-0}" != "0" ]; then
        args+=(--max-turns "$FOREMAN_MAX_TURNS")
    fi
    if [ -n "$resume_ref" ]; then
        args+=(--resume "$resume_ref")
    fi

    # Stream to the log; capture SESSION_REF from the first event carrying a
    # session_id. Only adapters advertising reliable cost reporting persist
    # total_cost_usd. pipefail carries claude's exit code out of the pipeline
    # (bash 3.2 compatible).
    claude "${args[@]}" <"$FOREMAN_PROMPT_FILE" | {
        local session_captured=0
        local line sid cost
        while IFS= read -r line; do
            printf '%s\n' "$line" >>"$FOREMAN_LOG_FILE"
            if [ "$session_captured" -eq 0 ]; then
                sid=$(printf '%s' "$line" | sed -n 's/.*"session_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
                if [ -n "$sid" ]; then
                    echo "SESSION_REF=$sid" >>"$FOREMAN_SESSION_FILE"
                    session_captured=1
                fi
            fi
            case " $FOREMAN_ADAPTER_CAPABILITIES " in
            *" cost "*)
                cost=$(printf '%s' "$line" | sed -n 's/.*"total_cost_usd"[[:space:]]*:[[:space:]]*\([0-9.]*\).*/\1/p')
                if [ -n "$cost" ]; then
                    echo "COST_USD=$cost" >>"$FOREMAN_SESSION_FILE"
                fi
                ;;
            esac
        done
    }
}
