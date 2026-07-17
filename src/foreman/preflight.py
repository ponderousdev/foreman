"""`foreman preflight` — the empirical security-assertion gate (#15).

Fine-grained GitHub token permissions cannot be introspected, so every
control is probed. For an API probe the call IS the write, so
"non-destructive" means BOUNDED: probes only ever touch scratch refs they
clean up, never the default branch; an unexpected success is confined to a
scratch ref, cleaned up best-effort, and fails preflight loudly with
explicit operator instructions where cleanup is impossible.

The five bot-tier assertions:
  1. the write token's authenticated login matches expected_login;
  2. the effective rules for the default branch include a pull-request
     requirement (rules-for-a-branch endpoint);
  3. the read token is proven unable to write (scratch ref only);
  4. the write token is proven unable to edit workflows — the control D3's
     relaxed separation leans on; the probe file is inert YAML with no
     `on:` trigger, so even an unexpected success cannot cause a run;
  5. the write token is proven unable to create, move, or delete version
     tags (D14) — creation with a scratch v* name; update and deletion
     against the sacrificial probe tag v0.0.0-probe (created at setup,
     never a release tag).

Operator tier — documented, not bot-asserted: whether any actor can BYPASS
the ruleset cannot be read with the bot's permissions and cannot be probed
without risking the very push the rule prevents. That audit is manual and
admin-credentialed, on a recorded cadence; run_preflight's output names it
so it cannot be forgotten silently.

Naming: v1's `preflight` (read-only issue analysis) is now `vet`; this
module owns the freed name.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass

from foreman.util import ForemanError, run

# (rc, stdout, stderr) from a `gh` invocation. Injected in tests.
GhCall = Callable[[list[str]], tuple[int, str, str]]

PROBE_TAG = "v0.0.0-probe"

# No trigger key and no jobs: even if the push unexpectedly succeeds, this
# file can never start a workflow run. (Deliberately written without the
# literal trigger keyword so the file cannot be armed by a careless edit.)
INERT_WORKFLOW_YAML = (
    "# foreman preflight probe — inert by design, with no trigger key and\n"
    "# no jobs. Its presence anywhere is a preflight failure being cleaned\n"
    "# up. Safe to delete.\n"
    "name: foreman-preflight-probe (inert)\n"
)

OPERATOR_TIER_NOTE = (
    "Operator tier (documented, NOT bot-asserted): audit the branch/tag "
    "ruleset bypass actors with admin credentials — confirm no bypass actor "
    "is one Foreman's write token can act as. Recorded cadence: alongside "
    "PAT rotation. See docs/architecture/security.md and "
    "docs/architecture/branch-protection.md."
)


@dataclass
class Probe:
    name: str
    ok: bool
    detail: str


def gh_with_token(token: str | None) -> GhCall:
    """A gh transport pinned to one token. None = Foreman's own (write)
    environment; a token string overrides GH_TOKEN (and clears
    GITHUB_TOKEN, which gh would otherwise prefer)."""

    def call(args: list[str]) -> tuple[int, str, str]:
        env = os.environ.copy()
        if token is not None:
            env["GH_TOKEN"] = token
            env.pop("GITHUB_TOKEN", None)
        proc = run(["gh", *args], check=False, env=env)
        return proc.returncode, proc.stdout, proc.stderr

    return call


def _api_json(call: GhCall, args: list[str]) -> tuple[int, object]:
    rc, out, _err = call(args)
    if rc != 0 or not out.strip():
        return rc, None
    try:
        return rc, json.loads(out)
    except json.JSONDecodeError:
        return rc, None


def _ref_sha(call: GhCall, slug: str, ref: str) -> str | None:
    rc, data = _api_json(call, ["api", f"repos/{slug}/git/ref/{ref}"])
    if rc != 0 or not isinstance(data, dict):
        return None
    return ((data.get("object") or {}).get("sha")) or None


def _create_ref(call: GhCall, slug: str, ref: str, sha: str) -> tuple[int, str]:
    rc, out, err = call(
        [
            "api",
            "--method",
            "POST",
            f"repos/{slug}/git/refs",
            "-f",
            f"ref={ref}",
            "-f",
            f"sha={sha}",
        ]
    )
    return rc, (err or out).strip()


def _delete_ref(call: GhCall, slug: str, ref: str) -> int:
    rc, _out, _err = call(["api", "--method", "DELETE", f"repos/{slug}/git/refs/{ref}"])
    return rc


# A probe PASSES only on an EXPLICIT authorization/ruleset denial — never on a
# transport error, 5xx, or malformed request, which would let a broken API
# masquerade as "cannot write" (fail-closed, CodeRabbit). GitHub answers a
# permission denial with 403 and a ruleset/branch-protection block with 422.
_DENIAL_MARKERS = (
    "HTTP 403",
    "HTTP 422",
    "Resource not accessible",
    "not permitted",
    "protected branch",
    "at least 1 approving review",
    "changes must be made through a pull request",
    "tag protection",
    "ruleset",
    "cannot be updated",
    "required status check",
)


def _is_denial(detail: str) -> bool:
    lowered = detail.lower()
    return any(marker.lower() in lowered for marker in _DENIAL_MARKERS)


def _denied_probe(name: str, detail: str, denied_msg: str) -> Probe:
    """Classify a rejected write: an explicit denial passes; any other error
    is inconclusive and FAILS (fail closed)."""
    if _is_denial(detail):
        return Probe(name, True, denied_msg)
    return Probe(
        name,
        False,
        f"the write was rejected, but not by a recognizable authorization or "
        f"ruleset denial — treating as INCONCLUSIVE (fail closed): {detail}",
    )


def _force_move_ref(call: GhCall, slug: str, ref: str, sha: str) -> tuple[int, str]:
    rc, out, err = call(
        [
            "api",
            "--method",
            "PATCH",
            f"repos/{slug}/git/refs/{ref}",
            "-f",
            f"sha={sha}",
            "-F",
            "force=true",
        ]
    )
    return rc, (err or out).strip()


def run_preflight(
    *,
    slug: str,
    default_branch: str,
    expected_login: str,
    write: GhCall,
    read: GhCall,
) -> list[Probe]:
    """All five bot-tier assertions. Every probe is bounded to a scratch ref
    it cleans up; none targets the default branch."""
    probes: list[Probe] = []
    suffix = secrets.token_hex(4)

    # 1 — identity: the wrong token must fail before any write.
    if not expected_login:
        probes.append(
            Probe(
                "write-token login",
                False,
                "expected_login is not set in .foreman.toml — preflight "
                "cannot assert identity",
            )
        )
    else:
        rc, out, _err = write(["api", "user", "--jq", ".login"])
        actual = out.strip()
        probes.append(
            Probe(
                "write-token login",
                rc == 0 and actual == expected_login,
                f"authenticated as '{actual or 'unknown'}', expected "
                f"'{expected_login}'",
            )
        )
    # Identity is the gate on every later probe: if the authenticated token is
    # not the expected one, running mutation probes could exercise refs,
    # workflows, and tags with the wrong — possibly privileged — token. Stop
    # here (CodeRabbit, critical).
    if not probes[-1].ok:
        return probes

    # 2 — the effective rules for the default branch require pull requests.
    rc, rules = _api_json(
        write, ["api", f"repos/{slug}/rules/branches/{default_branch}"]
    )
    has_pr_rule = (
        rc == 0
        and isinstance(rules, list)
        and any(
            isinstance(rule, dict) and rule.get("type") == "pull_request"
            for rule in rules
        )
    )
    probes.append(
        Probe(
            "default branch requires PRs",
            has_pr_rule,
            (
                f"rules-for-a-branch on '{default_branch}' "
                + (
                    "include a pull_request rule"
                    if has_pr_rule
                    else "include NO pull_request rule (or the endpoint failed) "
                    "— direct pushes to the default branch may be possible"
                )
            ),
        )
    )

    main_sha = _ref_sha(write, slug, f"heads/{default_branch}")
    if main_sha is None:
        probes.append(
            Probe(
                "resolve default branch",
                False,
                f"cannot resolve heads/{default_branch} — remaining probes "
                "skipped (they need a base sha)",
            )
        )
        return probes

    probes.append(_probe_read_token_cannot_write(slug, read, write, main_sha, suffix))
    probes.append(_probe_no_workflow_edit(slug, write, main_sha, suffix))
    probes.extend(_probe_tag_immutability(slug, write, main_sha, suffix))
    return probes


def _probe_read_token_cannot_write(
    slug: str, read: GhCall, write: GhCall, main_sha: str, suffix: str
) -> Probe:
    scratch = f"refs/heads/foreman-preflight-read-probe-{suffix}"
    rc, detail = _create_ref(read, slug, scratch, main_sha)
    if rc != 0:
        return _denied_probe(
            "read token cannot write",
            detail,
            "read-token ref creation was rejected as expected",
        )
    # Unexpected success: the write already happened — bounded to the
    # scratch ref. Clean it up (the read token just proved it can) and fail.
    cleanup_rc = _delete_ref(read, slug, scratch)
    if cleanup_rc != 0:
        cleanup_rc = _delete_ref(write, slug, scratch)
    return Probe(
        "read token cannot write",
        False,
        "the READ token created a ref — it has write permission it must "
        f"not have (scratch {scratch} "
        + ("cleaned up" if cleanup_rc == 0 else "LEFT BEHIND — delete it")
        + "); rotate or rescope the agent token",
    )


def _probe_no_workflow_edit(
    slug: str, write: GhCall, main_sha: str, suffix: str
) -> Probe:
    scratch_branch = f"foreman-preflight-wf-probe-{suffix}"
    scratch_ref = f"refs/heads/{scratch_branch}"
    rc, detail = _create_ref(write, slug, scratch_ref, main_sha)
    if rc != 0:
        return Probe(
            "write token cannot edit workflows",
            False,
            f"could not create the scratch branch for the probe ({detail}) — "
            "workflow-edit denial NOT proven",
        )
    try:
        content = base64.b64encode(INERT_WORKFLOW_YAML.encode()).decode()
        push_rc, push_detail = _put_file(
            write,
            slug,
            path=".github/workflows/foreman-preflight-probe.yml",
            branch=scratch_branch,
            content_b64=content,
        )
        if push_rc != 0:
            return _denied_probe(
                "write token cannot edit workflows",
                push_detail,
                "workflow-file push was rejected as expected (no workflows "
                "permission) — D3's load-bearing control holds",
            )
        # Unexpected success — clean up the scratch branch and report whether
        # the cleanup actually landed (never claim a teardown that failed).
        cleanup_rc = _delete_ref(write, slug, scratch_ref)
        cleaned = (
            "scratch branch deleted"
            if cleanup_rc == 0
            else (f"scratch branch {scratch_ref} LEFT BEHIND (delete it manually)")
        )
        return Probe(
            "write token cannot edit workflows",
            False,
            "the write token PUSHED a workflow file (inert YAML, no trigger "
            f"key — it cannot run); {cleaned}. The PAT has workflow permission "
            "it must not have. Rescope the PAT NOW: a prompt-injected agent "
            "could otherwise reach Actions secrets",
        )
    finally:
        _delete_ref(write, slug, scratch_ref)


def _put_file(
    write: GhCall, slug: str, *, path: str, branch: str, content_b64: str
) -> tuple[int, str]:
    rc, out, err = write(
        [
            "api",
            "--method",
            "PUT",
            f"repos/{slug}/contents/{path}",
            "-f",
            "message=chore: foreman preflight probe (inert; auto-cleaned)",
            "-f",
            f"content={content_b64}",
            "-f",
            f"branch={branch}",
        ]
    )
    return rc, (err or out).strip()


def _probe_tag_immutability(
    slug: str, write: GhCall, main_sha: str, suffix: str
) -> list[Probe]:
    probes: list[Probe] = []

    # 5a — creation: a scratch v* name, never the probe tag (creation could
    # succeed, and the probe tag must stay exactly where setup put it).
    scratch_tag = f"refs/tags/v0.0.0-probe-{suffix}"
    rc, detail = _create_ref(write, slug, scratch_tag, main_sha)
    if rc != 0:
        probes.append(
            _denied_probe(
                "write token cannot create version tags",
                detail,
                "v* tag creation was rejected as expected (creation ruleset)",
            )
        )
    else:
        cleanup_rc = _delete_ref(write, slug, scratch_tag)
        probes.append(
            Probe(
                "write token cannot create version tags",
                False,
                f"the write token CREATED {scratch_tag} "
                + ("(cleaned up)" if cleanup_rc == 0 else "(LEFT BEHIND — delete it)")
                + " — the tag-creation ruleset is not protecting v*; a moved "
                "version tag is code execution in every consumer (D14)",
            )
        )

    # 5b/5c — update and deletion, against the standing sacrificial tag.
    probe_sha = _ref_sha(write, slug, f"tags/{PROBE_TAG}")
    if probe_sha is None:
        probes.append(
            Probe(
                "write token cannot move or delete version tags",
                False,
                f"sacrificial probe tag '{PROBE_TAG}' does not exist — "
                "operator action: create it once at setup with admin "
                f"credentials (git tag {PROBE_TAG} <any sha> && git push "
                f"origin {PROBE_TAG}); it is never a release tag",
            )
        )
        return probes

    move_target = (
        main_sha if probe_sha != main_sha else _parent_sha(write, slug, main_sha)
    )
    if not move_target:
        probes.append(
            Probe(
                "write token cannot move version tags",
                False,
                "no distinct sha available to attempt the move probe against",
            )
        )
    else:
        rc, detail = _force_move_ref(write, slug, f"tags/{PROBE_TAG}", move_target)
        if rc != 0:
            probes.append(
                _denied_probe(
                    "write token cannot move version tags",
                    detail,
                    "force-moving the probe tag was rejected as expected "
                    "(immutability ruleset, no bypass actors)",
                )
            )
        else:
            restore_rc, _restore_detail = _force_move_ref(
                write, slug, f"tags/{PROBE_TAG}", probe_sha
            )
            probes.append(
                Probe(
                    "write token cannot move version tags",
                    False,
                    f"the write token MOVED {PROBE_TAG} "
                    + (
                        f"(restored to {probe_sha[:12]})"
                        if restore_rc == 0
                        else f"(RESTORE FAILED — operator: reset it to {probe_sha})"
                    )
                    + " — tag immutability is NOT enforced; every consumer's "
                    "pin is at risk (D14)",
                )
            )

    rc, _out, err = write(
        ["api", "--method", "DELETE", f"repos/{slug}/git/refs/tags/{PROBE_TAG}"]
    )
    if rc != 0:
        probes.append(
            _denied_probe(
                "write token cannot delete version tags",
                err.strip(),
                "deleting the probe tag was rejected as expected",
            )
        )
    else:
        recreate_rc, _detail = _create_ref(
            write, slug, f"refs/tags/{PROBE_TAG}", probe_sha
        )
        probes.append(
            Probe(
                "write token cannot delete version tags",
                False,
                f"the write token DELETED {PROBE_TAG}"
                + (
                    " (recreated)"
                    if recreate_rc == 0
                    else (
                        " and cannot recreate it (creation is denied) — "
                        f"operator: recreate {PROBE_TAG} at {probe_sha} with "
                        "admin credentials"
                    )
                )
                + " — tag deletion is NOT blocked (D14)",
            )
        )
    return probes


def _parent_sha(call: GhCall, slug: str, sha: str) -> str | None:
    rc, data = _api_json(call, ["api", f"repos/{slug}/commits/{sha}"])
    if rc != 0 or not isinstance(data, dict):
        return None
    parents = data.get("parents") or []
    if parents and isinstance(parents[0], dict):
        return parents[0].get("sha")
    return None


def render(probes: list[Probe]) -> str:
    lines = ["Preflight assertions (empirical; scratch-ref bounded):", ""]
    for probe in probes:
        mark = "PASS" if probe.ok else "FAIL"
        lines.append(f"  [{mark}] {probe.name}")
        lines.append(f"         {probe.detail}")
    lines.append("")
    lines.append(OPERATOR_TIER_NOTE)
    return "\n".join(lines)


def read_token_from_env() -> str:
    token = os.environ.get("FOREMAN_AGENT_GH_TOKEN", "")
    if not token:
        raise ForemanError(
            "FOREMAN_AGENT_GH_TOKEN is not set — preflight probes the READ "
            "token's boundaries and cannot run without it"
        )
    return token
