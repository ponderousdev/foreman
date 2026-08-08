"""Agent backend adapter seam. Adapters are `backends/<name>.sh` — the entire
vendor surface. Foreman shells out; no vendor SDKs in core.

Adapter contract:
  argv:  <adapter> run | resume <session-ref> |
         attach [session-ref | --session-file <path>] | capabilities
  cwd:   the unit's worktree
  env:   FOREMAN_PROMPT_FILE, FOREMAN_RESULT_FILE, FOREMAN_SESSION_FILE,
         FOREMAN_LOG_FILE, FOREMAN_TIMEOUT_MIN, FOREMAN_PERMISSION_MODE,
         FOREMAN_BILLING, FOREMAN_MAX_TURNS (0 = uncapped)
  out:   exit 0/non-zero; session file line `SESSION_REF=<id>` written as
         EARLY as the backend allows (killed agents emit no final event —
         resume depends on this), later `COST_USD=<x>` when known.
  caps:  `capabilities` prints tokens, e.g. `resume cost`.

Execution goes through the Runner seam: this module builds the UnitSpec
(including the TOTAL agent environment) and drives spawn → wait → kill;
where the adapter actually runs is the runner's business. Timeouts are
enforced HERE (portable), not in the adapters.

The agent environment is an allowlist (#13): PATH/HOME/USER/LANG/TERM,
CLAUDE_CODE_OAUTH_TOKEN, the READ-ONLY GitHub token (from
FOREMAN_AGENT_GH_TOKEN, handed to the agent as GH_TOKEN), and explicit
FOREMAN_* variables. Never the write token, never raw provider variables such
as ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or DEEPSEEK_API_KEY. Adapters
translate their dedicated FOREMAN_* provisioning variable and strip unrelated
provider credentials before launching the CLI. Under local this is defense in
depth, not containment (D1/D3): the write token remains reachable on the shared
box, and no test may claim otherwise.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from foreman import progress as progress_mod
from foreman import runner as runner_mod
from foreman import signatures as signatures_mod
from foreman.config import Config
from foreman.runner import Runner, UnitSpec, WaitTimeout
from foreman.util import ForemanError, run, tail, utc_now_iso, write_text

BACKENDS_DIR = Path(__file__).resolve().parent / "backends"

RESULT_STATUSES = ("completed", "blocked")

# Environment names copied verbatim from Foreman's environment when present.
AGENT_ENV_BASE = ("PATH", "HOME", "USER", "LANG", "TERM")
# The read-only token Foreman's operator provisions for agents; handed to
# the unit as GH_TOKEN. Required before any dispatch (#13).
AGENT_TOKEN_VAR = "FOREMAN_AGENT_GH_TOKEN"
# The ONLY host-side FOREMAN_* variables forwarded to the unit — an explicit
# allowlist, not a prefix sweep, so a new operator-only FOREMAN_* control or
# secret is never exposed to the agent by default (#13, spec "explicit
# FOREMAN_* variables"). Foreman's per-unit FOREMAN_* values (prompt/result/
# session/log paths, timeout, billing, …) are layered on by run_backend and
# are not host-inherited. Dedicated provider credentials are explicit entries;
# each adapter validates and consumes only its own. Under other backends or
# subscription billing they are simply absent/unused.
# FOREMAN_READONLY is included so vet's read-only mode actually reaches the
# adapter (claude.sh branches on it) — the total-allowlist env otherwise
# silently dropped it, and vet ran with normal write permissions.
AGENT_FOREMAN_ENV = (
    "FOREMAN_ANTHROPIC_API_KEY",
    "FOREMAN_DEEPSEEK_API_KEY",
    "FOREMAN_READONLY",
)
# Reaping window after kill(): the group is already dead or dying; this only
# bounds how long we wait for the recorded status to become readable.
KILL_REAP_S = 30


@dataclass
class BackendResult:
    returncode: int | None
    timed_out: bool = False
    abnormal: bool = False  # dead with no recorded status — never a real exit
    session_ref: str | None = None
    cost_usd: float | None = None
    quota_wait: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.abnormal


def adapter_path(name: str) -> Path:
    # Select from enumerated trusted basenames instead of joining untrusted
    # recorded text into a path. A unit may corrupt its own telemetry, but it
    # must never turn shepherd/attach into an arbitrary-script launcher.
    available = {path.stem: path for path in BACKENDS_DIR.glob("*.sh")}
    path = available.get(name)
    if path is None:
        raise ForemanError(
            f"unknown backend '{name}' (available: {', '.join(sorted(available))})"
        )
    return path


def capabilities(adapter: Path) -> set[str]:
    proc = run([str(adapter), "capabilities"], check=False)
    return set(proc.stdout.split()) if proc.returncode == 0 else set()


def _uses_claude_cli(backend_name: str) -> bool:
    """Harness classification without provider-specific core branches."""
    return backend_name == "claude" or backend_name.startswith("claude-code-")


def backend_cli_version(cfg: Config, backend_name: str | None = None) -> str:
    """Best-effort agent-CLI version (recorded in run_started; asserted by
    assert_backend_version when pinned)."""
    if not _uses_claude_cli(backend_name or cfg.backend):
        return ""
    proc = run(["claude", "--version"], check=False)
    if proc.returncode != 0 or not proc.stdout:
        return ""
    return proc.stdout.strip().split()[0]


def assert_backend_version(cfg: Config, backend_name: str | None = None) -> None:
    """Pin check: headless behavior drifts between agent-CLI releases."""
    selected = backend_name or cfg.backend
    if not cfg.backend_version or not _uses_claude_cli(selected):
        return
    version = backend_cli_version(cfg, selected)
    if not version.startswith(cfg.backend_version):
        raise ForemanError(
            f"backend '{selected}' version mismatch: claude CLI is "
            f"'{version or 'missing'}', "
            f"config pins '{cfg.backend_version}'"
        )


def unit_dir(cfg: Config, root: Path, number: int) -> Path:
    path = root / cfg.runtime_dir / "units" / str(number)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _backend_selection_path(cfg: Config, root: Path, unit: int) -> Path:
    return runner_mod.runs_dir(cfg, root) / f"{unit}.backend"


def _record_backend_selection(
    cfg: Config, root: Path, unit: int, backend_name: str
) -> None:
    """Atomically persist the supervisor-owned effective adapter selection."""
    adapter_path(backend_name)  # known basename under BACKENDS_DIR
    path = _backend_selection_path(cfg, root, unit)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".backend.tmp")
    tmp.write_text(backend_name + "\n", encoding="utf-8")
    tmp.replace(path)


def recorded_backend(cfg: Config, root: Path, unit: int, fallback: str) -> str:
    """Return the trusted adapter selection that actually started this unit.

    The selector lives beside runner handles, outside the run directory granted
    to the agent. Runs created before selector recording fall back to the
    repository default. Once a selector exists, unknown names fail closed.
    """
    path = _backend_selection_path(cfg, root, unit)
    if not path.exists():
        return fallback
    try:
        selected = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ForemanError(f"invalid backend selection record {path}: {exc}") from exc
    if not selected:
        raise ForemanError(f"backend selection record {path} is empty")
    adapter_path(selected)  # reject traversal and unknown adapter names
    return selected


def agent_env(cfg: Config) -> dict[str, str]:
    """The TOTAL environment a unit receives (#13) — the runner passes it
    verbatim, never merged with os.environ. See the module docstring."""
    env: dict[str, str] = {}
    for name in (*AGENT_ENV_BASE, *AGENT_FOREMAN_ENV):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    read_token = os.environ.get(AGENT_TOKEN_VAR, "")
    if not read_token:
        raise ForemanError(
            f"{AGENT_TOKEN_VAR} is not set — agents receive a separate "
            "read-only GitHub token, never Foreman's write token (#13). "
            "Provision a fine-grained read-only PAT and export it as "
            f"{AGENT_TOKEN_VAR} in the bot devcontainer env."
        )
    env["GH_TOKEN"] = read_token
    oauth = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if oauth:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth
    return env


def _record_run_started(
    cfg: Config,
    unit_run_dir: Path,
    handle: runner_mod.Handle,
    spec: UnitSpec,
    *,
    backend_name: str,
    resume_ref: str | None,
) -> None:
    """#22: run_started records runner, image digest, and CLI version."""
    payload = {
        "schema": 1,
        "unit": spec.unit,
        "runner": handle.runner,
        "image": spec.image or None,
        "image_digest": None,  # local boots no image; sprite records one (v2.1)
        "backend": backend_name,
        "backend_cli_version": backend_cli_version(cfg, backend_name),
        "resume_ref": resume_ref,
        "timeout_s": spec.timeout_s,
        "started_at": utc_now_iso(),
    }
    write_text(unit_run_dir / "run_started.json", json.dumps(payload, indent=2) + "\n")


def _publish_started_run(
    cfg: Config,
    root: Path,
    unit_run_dir: Path,
    handle: runner_mod.Handle,
    spec: UnitSpec,
    *,
    backend_name: str,
    resume_ref: str | None,
) -> None:
    """Publish restart state in dependency order.

    The effective adapter becomes durable before the recoverable handle is
    visible. The agent-writable run_started document remains telemetry only.
    """
    _record_backend_selection(cfg, root, spec.unit, backend_name)
    runner_mod.save_handle(cfg, root, handle)
    _record_run_started(
        cfg,
        unit_run_dir,
        handle,
        spec,
        backend_name=backend_name,
        resume_ref=resume_ref,
    )


def run_backend(
    cfg: Config,
    root: Path,
    runner: Runner,
    adapter: Path,
    *,
    unit_number: int,
    cwd: Path,
    unit_run_dir: Path,
    prompt_file: Path,
    timeout_min: int,
    resume_ref: str | None = None,
    gate_cmds: list[list[str]] | None = None,
    reporter: progress_mod.UnitProgress | None = None,
) -> BackendResult:
    session_file = unit_run_dir / "session"
    log_file = unit_run_dir / "agent.log"
    result_file = unit_run_dir / "result.json"
    if result_file.exists():
        result_file.unlink()

    env = agent_env(cfg)
    env.update(
        {
            "FOREMAN_PROMPT_FILE": str(prompt_file),
            "FOREMAN_RESULT_FILE": str(result_file),
            "FOREMAN_SESSION_FILE": str(session_file),
            "FOREMAN_LOG_FILE": str(log_file),
            "FOREMAN_TIMEOUT_MIN": str(timeout_min),
            "FOREMAN_PERMISSION_MODE": cfg.resolved_permission_mode(),
            "FOREMAN_BILLING": cfg.billing,
            "FOREMAN_MAX_TURNS": str(cfg.max_turns),
        }
    )
    argv = [str(adapter), "resume", resume_ref] if resume_ref else [str(adapter), "run"]
    spec = UnitSpec(
        unit=unit_number,
        workdir=cwd,
        run_dir=unit_run_dir,
        env=env,
        cmd=tuple(argv),
        timeout_s=timeout_min * 60,
        # The composed gate rides the spec: that is the defined transport for
        # guest-executed gates (#29 → #30). Local ignores it — Foreman's own
        # process runs the gate in the worktree after the agent exits.
        gate_cmds=tuple(tuple(c) for c in (gate_cmds or [])),
    )
    # Snapshot the session file BEFORE the spawn (#54): the file persists
    # across resumes, and a run that dies before emitting its own COST_USD
    # must not be billed the previous invocation's last cost line.
    session_offset = session_file.stat().st_size if session_file.exists() else 0

    handle = runner.spawn(spec)
    _publish_started_run(
        cfg,
        root,
        unit_run_dir,
        handle,
        spec,
        backend_name=adapter.stem,
        resume_ref=resume_ref,
    )

    timed_out = False
    try:
        if reporter is not None:
            # Slice the otherwise-silent wait so a liveness indicator fires
            # while the agent runs (#83). The kill-on-timeout path below is
            # unchanged: wait_with_heartbeat raises WaitTimeout at the same
            # deadline runner.wait would have.
            rep = reporter
            status = progress_mod.wait_with_heartbeat(
                lambda s: runner.wait(handle, s),
                spec.timeout_s,
                lambda elapsed: rep.heartbeat(elapsed, spec.timeout_s),
                now=rep.now,
            )
        else:
            status = runner.wait(handle, spec.timeout_s)
    except WaitTimeout:
        timed_out = True
        runner.kill(handle)
        status = runner.wait(handle, KILL_REAP_S)
    finally:
        if reporter is not None:
            reporter.settle()

    return result_from_wait(
        unit_run_dir, status, timed_out=timed_out, session_offset=session_offset
    )


def remaining_timeout_s(unit_run_dir: Path, full_timeout_s: int) -> int:
    """Seconds left on the ORIGINAL dispatch deadline (#22): full timeout
    minus the elapsed time since run_started.started_at. Falls back to the
    full timeout when the record is missing or unparseable — never negative."""
    started_path = unit_run_dir / "run_started.json"
    if not started_path.exists():
        return full_timeout_s
    try:
        started_raw = json.loads(started_path.read_text(encoding="utf-8"))["started_at"]
        started = datetime.fromisoformat(started_raw)
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        return full_timeout_s
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    return max(0, int(full_timeout_s - elapsed))


def result_from_wait(
    unit_run_dir: Path,
    status: runner_mod.ExitStatus,
    *,
    timed_out: bool,
    session_offset: int = 0,
) -> BackendResult:
    """BackendResult from a wait() outcome — used by run_backend and by
    reattachment, where a restarted Foreman adopts a unit it never spawned
    and the recorded status is the only ground truth (#22). `session_offset`
    is the session file's size before this run spawned: COST_USD lines are
    parsed only past it, so a run that died before emitting cost is never
    billed a previous invocation's line (#54). SESSION_REF still resolves
    from the whole file — resuming depends on the earliest recorded ref."""
    result = BackendResult(
        returncode=status.code, timed_out=timed_out, abnormal=status.abnormal
    )
    _read_session_file(unit_run_dir / "session", result, cost_offset=session_offset)
    log_tail = (
        tail(unit_run_dir / "agent.log", 80)
        + "\n"
        + tail(unit_run_dir / "adapter-stdout.log", 40)
    )
    sig = signatures_mod.match(log_tail, signatures_mod.load())
    if sig is not None and sig.action == "quota_wait":
        result.quota_wait = True
    return result


def _read_session_file(
    session_file: Path, result: BackendResult, *, cost_offset: int = 0
) -> None:
    if not session_file.exists():
        return
    text = session_file.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("SESSION_REF=") and not result.session_ref:
            result.session_ref = line.split("=", 1)[1].strip() or None
    for line in text[cost_offset:].splitlines():
        if line.startswith("COST_USD="):
            try:
                result.cost_usd = float(line.split("=", 1)[1])
            except ValueError:
                pass


# ── adjudication sidecar (#46) ───────────────────────────────────────

ADJUDICATION_FILE = "adjudication.json"
DISPOSITIONS = ("applied", "declined")


@dataclass
class ThreadDisposition:
    thread_id: str
    disposition: str  # applied | declined
    note: str
    # For `applied`: the commit the note names, parsed here; the shepherd
    # verifies it exists on the branch before authorizing the thread writes.
    applied_sha: str | None = None


_APPLIED_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


def read_adjudication(
    unit_run_dir: Path,
) -> tuple[list[ThreadDisposition] | None, list[str]]:
    """Validate the adjudication sidecar. The agent only RECORDS
    dispositions here — its token is read-only (#13), so foreman posts each
    note as the thread reply and resolves the thread, keeping every GitHub
    write behind the guarded mutation seam (#46)."""
    path = unit_run_dir / ADJUDICATION_FILE
    if not path.exists():
        return None, ["agent exited without writing the adjudication sidecar"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, [f"{ADJUDICATION_FILE} is not valid JSON: {exc}"]
    if not isinstance(data, dict):
        return None, [f"{ADJUDICATION_FILE} must be a JSON object"]
    errors: list[str] = []
    if data.get("schema") != 1:
        errors.append(f"{ADJUDICATION_FILE}: schema must be 1")
    raw = data.get("dispositions")
    if not isinstance(raw, list) or not raw:
        errors.append(f"{ADJUDICATION_FILE}: dispositions must be a non-empty list")
        return None, errors
    out: list[ThreadDisposition] = []
    for index, entry in enumerate(raw):
        where = f"{ADJUDICATION_FILE}: dispositions[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{where} must be an object")
            continue
        thread_id = entry.get("thread_id")
        disposition = entry.get("disposition")
        note = entry.get("note")
        if not isinstance(thread_id, str) or not thread_id.strip():
            errors.append(f"{where}.thread_id must be a non-empty string")
            continue
        if disposition not in DISPOSITIONS:
            errors.append(f"{where}.disposition must be one of {DISPOSITIONS}")
            continue
        if not isinstance(note, str) or not note.strip():
            errors.append(f"{where}.note must be a non-empty string")
            continue
        applied_sha = None
        if disposition == "applied":
            # An applied claim must name its commit (`applied in <sha>`) so
            # the shepherd can prove the fix exists before resolving.
            match = _APPLIED_SHA_RE.search(note)
            if match is None:
                errors.append(
                    f"{where}.note must name the commit for an applied "
                    "disposition (`applied in <short-sha>`)"
                )
                continue
            applied_sha = match.group(0)
        out.append(
            ThreadDisposition(thread_id.strip(), disposition, note.strip(), applied_sha)
        )
    if len({d.thread_id for d in out}) != len(out):
        errors.append(f"{ADJUDICATION_FILE}: duplicate thread_id entries")
    return (out, []) if not errors else (None, errors)


# ── result contract ──────────────────────────────────────────────────


@dataclass
class ResultContract:
    status: str
    summary: str = ""
    handoff: str = ""
    human_tasks: list[str] = field(default_factory=list)
    proposed_pr_title: str = ""
    ac_test_map: list[dict] = field(default_factory=list)
    blocked_question: str | None = None


def read_result(
    unit_run_dir: Path, worktree: Path
) -> tuple[ResultContract | None, list[str]]:
    """Validate the sidecar result contract; BLOCKED.md is the fallback signal."""
    result_file = unit_run_dir / "result.json"
    blocked_md = worktree / "BLOCKED.md"
    if not result_file.exists():
        if blocked_md.exists():
            question = blocked_md.read_text(encoding="utf-8").strip()
            return ResultContract(status="blocked", blocked_question=question), []
        return None, ["agent exited without writing the result contract"]
    try:
        data = json.loads(result_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, [f"result.json is not valid JSON: {exc}"]

    errors: list[str] = []
    if not isinstance(data, dict):
        return None, ["result.json must be a JSON object"]
    if data.get("schema") != 1:
        errors.append("result.json: schema must be 1")
    status = data.get("status")
    if status not in RESULT_STATUSES:
        errors.append(f"result.json: status must be one of {RESULT_STATUSES}")
        return None, errors

    contract = ResultContract(status=status)
    contract.summary = _expect_str(
        data, "summary", errors, required=(status == "completed")
    )
    contract.handoff = _expect_str(
        data, "handoff", errors, required=(status == "completed")
    )
    contract.proposed_pr_title = _expect_str(
        data, "proposed_pr_title", errors, required=False
    )
    contract.blocked_question = data.get("blocked_question") or None
    if status == "blocked" and not contract.blocked_question:
        if blocked_md.exists():
            contract.blocked_question = blocked_md.read_text(encoding="utf-8").strip()
        else:
            errors.append("result.json: blocked status requires blocked_question")
    tasks = data.get("human_tasks", [])
    if not isinstance(tasks, list) or not all(isinstance(t, str) for t in tasks):
        errors.append("result.json: human_tasks must be a list of strings")
    else:
        contract.human_tasks = tasks
    ac_map = data.get("ac_test_map", [])
    if not isinstance(ac_map, list):
        errors.append("result.json: ac_test_map must be a list")
    else:
        for entry in ac_map:
            if (
                not isinstance(entry, dict)
                or "criterion" not in entry
                or "tests" not in entry
            ):
                errors.append(
                    "result.json: ac_test_map entries need {criterion, tests}"
                )
                break
        else:
            contract.ac_test_map = ac_map
    if status == "completed" and not contract.ac_test_map:
        errors.append("result.json: completed status requires a non-empty ac_test_map")
    return (contract, errors) if not errors else (None, errors)


def _expect_str(data: dict, key: str, errors: list[str], *, required: bool) -> str:
    value = data.get(key, "")
    if not isinstance(value, str):
        errors.append(f"result.json: {key} must be a string")
        return ""
    if required and not value.strip():
        errors.append(f"result.json: {key} is required")
    return value


def write_resume_state(unit_run_dir: Path, worktree: Path, note: str) -> Path:
    """Deterministic resume-state so a later resume needs no archaeology."""
    status = run(
        ["git", "-C", str(worktree), "status", "--porcelain", "-b"], check=False
    ).stdout
    log = run(
        ["git", "-C", str(worktree), "log", "--oneline", "-5"], check=False
    ).stdout
    session = (
        (unit_run_dir / "session").read_text(encoding="utf-8")
        if (unit_run_dir / "session").exists()
        else ""
    )
    body = (
        f"# Resume state — {utc_now_iso()}\n\n{note}\n\n"
        f"## Worktree\n\n`{worktree}`\n\n```text\n{status}```\n\n"
        f"## Recent commits\n\n```text\n{log}```\n\n"
        f"## Session\n\n```text\n{session}```\n\n"
        f"## Agent log tail\n\n```text\n{tail(unit_run_dir / 'agent.log', 40)}\n```\n"
    )
    path = unit_run_dir / "resume-state.md"
    write_text(path, body)
    return path
