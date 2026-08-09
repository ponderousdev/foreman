"""Per-unit dispatch pipeline: isolate → prompt → agent (through the Runner
seam) → verify → freshness gate → push → PR → status comment. Bounded
concurrency across units.

Idempotent by derivation: a unit with an existing attempt branch or open PR
is skipped — no state file records "dispatched"; git and GitHub do. Handles
under .foreman/runs/ are a cache for reattachment, never the truth (#22):
after a crash, a rerun re-derives state from GitHub and git, takes the
per-unit lock, probes handle liveness (PID + start-time), and reattaches
rather than redispatching. A dead unit's exit status is read from the
recorded status file; dead-with-no-status is reported as abnormal, never
guessed.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from foreman import backend as backend_mod
from foreman import gate, report, spec, verify, worktree
from foreman import handoff as handoff_mod
from foreman import pr as pr_mod
from foreman import progress as progress_mod
from foreman import runner as runner_mod
from foreman import signatures as signatures_mod
from foreman import trust as trust_mod
from foreman.config import Config
from foreman.github import GitHub
from foreman.graph import Target, Unit, dependency_satisfied
from foreman.runner import Selection, WaitTimeout
from foreman.util import ForemanError, info, warn, write_text

RETRIGGER_SUBJECT = "chore: retrigger ci (foreman)"


def _gate_handoffs(
    gh: GitHub,
    cfg: Config,
    selection: Selection,
    handoffs: list[tuple[int, str]],
) -> tuple[list[tuple[int, str]], list[int]]:
    """#46: a handoff inherits its dependency's origin classification. The
    text is agent-generated from that issue's content — and the origin's
    contributors may have lost access since, so current repo trust cannot
    attest it. A handoff whose origin carries untrusted contributions is
    withheld on a runner lacking the untrusted-input boundary, and the
    withholding is disclosed to the agent — excluded like untrusted
    comments, never silently."""
    kept: list[tuple[int, str]] = []
    withheld: list[int] = []
    if not handoffs:
        return kept, withheld
    repo = trust_mod.repo_trust(gh, cfg)
    for dep, text in handoffs:
        origin = trust_mod.classify_branch_origin(gh, cfg, dep)
        if selection.refusal(trust_mod.required_for(cfg, repo, origin)):
            withheld.append(dep)
        else:
            kept.append((dep, text))
    return kept, withheld


@dataclass
class Outcome:
    unit: Unit
    status: (
        str  # pr-open | failed | blocked | waiting | skipped | held | not-armed | stale
    )
    branch: str = ""
    pr_url: str = ""
    detail: str = ""
    cost_usd: float | None = None
    duration_s: float = 0.0

    @property
    def dispatched(self) -> bool:
        return self.status == "pr-open"


def eligibility(
    gh: GitHub, cfg: Config, target: Target, selection: Selection
) -> tuple[list[Unit], list[Outcome]]:
    """Split units into ready-to-dispatch and skipped (with reasons).

    Trust and capability checks consume the unit's classification and the
    selection's refusal composer — never a runner name (the leak test keeps
    it that way)."""
    ready: list[Unit] = []
    skipped: list[Outcome] = []
    remote_name = worktree.remote(cfg)
    for number in sorted(target.units):
        unit = target.units[number]
        inp = unit.inputs
        assert inp is not None, "inputs must be resolved before eligibility"
        if not unit.open:
            skipped.append(Outcome(unit, "skipped", detail="already closed"))
            continue
        if unit.errors or (inp and inp.errors):
            problems = "; ".join(unit.errors + inp.errors)
            skipped.append(Outcome(unit, "failed", detail=f"contract: {problems}"))
            continue
        if inp.hold:
            skipped.append(Outcome(unit, "held", detail="foreman=hold"))
            continue
        if not inp.armed:
            skipped.append(
                Outcome(unit, "not-armed", detail="no foreman approval input")
            )
            continue
        if unit.trust is not None and unit.trust.refusals:
            skipped.append(
                Outcome(unit, "refused", detail="; ".join(unit.trust.refusals))
            )
            continue
        cap_message = selection.refusal(unit.required_capabilities)
        if cap_message:
            detail = cap_message
            if unit.trust is not None and unit.trust.contributors:
                detail += " (untrusted contributions: " + ", ".join(
                    unit.trust.contributors[:3]
                )
                if len(unit.trust.contributors) > 3:
                    detail += ", …"
                detail += ")"
            skipped.append(Outcome(unit, "refused", detail=detail))
            continue
        spec_info = spec.validate(unit)
        if spec_info.errors:
            skipped.append(Outcome(unit, "failed", detail="; ".join(spec_info.errors)))
            continue
        unmet = []
        for dep in unit.blocked_by:
            dep_inputs = target.units[dep].inputs if dep in target.units else None
            done = dependency_satisfied(
                gh, cfg, dep, inputs=dep_inputs, mode=target.mode
            )
            if not done.satisfied:
                unmet.append(f"#{dep} ({done.how})")
        if unmet:
            skipped.append(
                Outcome(unit, "waiting", detail=f"blocked by {', '.join(unmet)}")
            )
            continue
        attempts = worktree.attempt_branches(cfg, remote_name, unit.number)
        open_prs = [p for p in gh.prs(state="open") if p["headRefName"] in attempts]
        if open_prs:
            skipped.append(
                Outcome(
                    unit,
                    "skipped",
                    branch=open_prs[0]["headRefName"],
                    pr_url=open_prs[0]["url"],
                    detail="open PR exists (in flight)",
                )
            )
            continue
        in_flight = [b for b in attempts if gh.branch_exists_remote(b)]
        if in_flight:
            skipped.append(
                Outcome(
                    unit,
                    "skipped",
                    branch=in_flight[0],
                    detail="attempt branch exists (in flight or awaiting retry/cleanup)",
                )
            )
            continue
        ready.append(unit)
    return ready, skipped


def _trust_drift_refusal(
    gh: GitHub, cfg: Config, selection: Selection, unit: Unit, mode: str | None
) -> str | None:
    """Re-derive the repo predicate and the unit's D13 classification against
    live GitHub, and return a refusal string if the configured runner can no
    longer satisfy the (possibly higher) requirement, or the arming/edit
    attestation broke since plan time. None means trust still holds.

    This is the TOCTOU guard for trust, mirroring the content freshness gate:
    the plan-time classification on `unit` is advisory; the spawn-time answer
    is authoritative and fails closed."""
    repo = trust_mod.repo_trust(gh, cfg)
    unit_trust = (
        trust_mod.classify_unit(gh, cfg, unit, mode or "labels") if unit.open else None
    )
    if unit_trust is not None and unit_trust.refusals:
        return "; ".join(unit_trust.refusals)
    required = trust_mod.required_for(cfg, repo, unit_trust)
    return selection.refusal(required)


def _timeout_min(cfg: Config, unit: Unit) -> int:
    inp = unit.inputs
    return inp.timeout_min if inp and inp.timeout_min else cfg.dispatch_timeout_min


def _write_dispatch_meta(
    run_dir: Path, *, recorded_hash: str, base_sha: str, branch: str
) -> None:
    write_text(
        run_dir / "dispatch-meta.txt",
        f"spec_hash={recorded_hash}\nbase_sha={base_sha}\nbranch={branch}\n",
    )


def _read_dispatch_meta(run_dir: Path) -> dict[str, str]:
    path = run_dir / "dispatch-meta.txt"
    if not path.exists():
        return {}
    meta: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            meta[key.strip()] = value.strip()
    return meta


def dispatch_unit(
    gh: GitHub,
    cfg: Config,
    root: Path,
    selection: Selection,
    unit: Unit,
    *,
    mode: str | None = None,
    progress: progress_mod.UnitProgress | None = None,
) -> Outcome:
    started = time.monotonic()
    # A standalone single-unit reporter when none was supplied (e.g. `foreman
    # retry`), so a direct dispatch narrates too. run_dispatch supplies its own
    # workers-aware reporter for the concurrent case.
    if progress is None:
        progress = progress_mod.DispatchReporter(workers=1).unit(unit.number)
    lock = runner_mod.UnitLock(cfg, root, unit.number)
    if not lock.acquire():
        # Lock held: another process is driving this unit — we did not start
        # it, so we do not acknowledge it (start() is post-lock, always).
        return Outcome(
            unit,
            "skipped",
            detail="unit lock held — another foreman process is driving this unit",
        )
    try:
        outcome = _dispatch_locked(
            gh, cfg, root, selection, unit, mode=mode, progress=progress
        )
    except Exception as exc:
        # #82: the initiation write has already advertised `dispatched` on the
        # issue — a setup failure after it must not strand that as the last
        # word. Record the failure, then let run_dispatch see the raise.
        _post_status(gh, unit, Outcome(unit, "failed", detail=str(exc)))
        raise
    finally:
        lock.release()
    outcome.duration_s = time.monotonic() - started
    _post_status(gh, unit, outcome)
    if outcome.status == "pr-open":
        # The PR is already open — best-effort cleanup must never turn a
        # successful dispatch into a reported failure. Log and move on.
        try:
            _forget_unit(cfg, root, selection, unit)
        except Exception as exc:  # noqa: BLE001 — cleanup is non-fatal
            warn(f"#{unit.number}: post-PR cleanup failed (PR is open): {exc}")
    return outcome


def _forget_unit(cfg: Config, root: Path, selection: Selection, unit: Unit) -> None:
    """PR branch is pushed: drop execution state. The shepherd recreates
    worktrees on demand; logs under .foreman/units/ remain."""
    handle = runner_mod.load_handle(cfg, root, unit.number)
    if handle is not None:
        selection.runner.cleanup(handle)
    runner_mod.delete_handle(cfg, root, unit.number)
    wt_path = worktree.worktree_path(cfg, root, unit)
    if wt_path.exists():
        worktree.remove(wt_path)


def _dispatch_locked(
    gh: GitHub,
    cfg: Config,
    root: Path,
    selection: Selection,
    unit: Unit,
    *,
    mode: str | None,
    progress: progress_mod.UnitProgress,
) -> Outcome:
    remote_name = worktree.remote(cfg)
    default_branch = gh.default_branch()
    base = f"{remote_name}/{default_branch}"
    wt_path = worktree.worktree_path(cfg, root, unit)
    run_dir = backend_mod.unit_dir(cfg, root, unit.number)

    if wt_path.exists():
        handle = runner_mod.load_handle(cfg, root, unit.number)
        if handle is not None:
            return _reattach_unit(
                gh,
                cfg,
                root,
                selection,
                unit,
                wt_path,
                run_dir,
                handle,
                base=base,
                mode=mode,
                progress=progress,
            )
        return Outcome(
            unit,
            "skipped",
            detail=f"worktree already exists ({wt_path}) — run foreman retry or cleanup",
        )

    # Re-evaluate trust at dispatch (#14 / D4): repo visibility, access, and
    # post-arming edits can all change in the window between plan and spawn.
    # Recompute the D4/D13 predicate against live GitHub and fail closed on
    # any drift — never spend tokens on a unit whose trust regressed.
    drift = _trust_drift_refusal(gh, cfg, selection, unit, mode)
    if drift is not None:
        return Outcome(unit, "refused", detail=f"trust re-check at dispatch: {drift}")

    inp = unit.inputs
    backend_name = inp.backend if inp and inp.backend else cfg.backend
    # The CLI's early check covers the repository default. Re-check the
    # effective per-unit selection here, after live trust validation and before
    # any worktree or agent process is created.
    backend_mod.assert_backend_version(cfg, backend_name)

    existing = worktree.attempt_branches(cfg, remote_name, unit.number)
    branch = worktree.next_attempt_branch(worktree.branch_name(cfg, unit), existing)

    # Immediate acknowledgment (#83 AC1): branch + live log path, emitted
    # post-lock and post-drift, before the GitHub-bound prompt assembly.
    progress.start(branch, run_dir / "agent.log")
    progress.phase("assembling prompt")
    comments, excluded = spec.trusted_comments(gh, cfg, unit.number)
    recorded_hash = spec.spec_hash(unit, comments)
    base_sha = worktree.base_sha(remote_name, default_branch)
    _write_dispatch_meta(
        run_dir, recorded_hash=recorded_hash, base_sha=base_sha, branch=branch
    )

    # #82: the issue shows foreman has taken it the moment work starts —
    # a time-stamped past fact in the status comment's event log, not stored
    # state (nothing here can be falsified by a crash).
    attempt = worktree.attempt_number(worktree.branch_name(cfg, unit), branch)
    report.update_status_comment(
        gh,
        report.UnitStatus(unit=unit, state="dispatched", branch=branch),
        event=f"initiated (attempt {attempt}, branch `{branch}`)",
    )

    advertised = selection.runner.capabilities()
    gate_cmds = gate.compose(cfg, advertised)
    handoffs, withheld_handoffs = _gate_handoffs(
        gh, cfg, selection, spec.collect_handoffs(gh, cfg, unit)
    )
    prompt_text = spec.assemble_dispatch_prompt(
        gh,
        cfg,
        unit,
        branch=branch,
        default_branch=default_branch,
        result_file=str(run_dir / "result.json"),
        comments=comments,
        excluded_comments=excluded,
        handoffs=handoffs,
        verify_display=gate.describe(gate_cmds),
        capabilities=advertised,
        withheld_handoffs=withheld_handoffs,
    )
    prompt_file = run_dir / "prompt.md"
    write_text(prompt_file, prompt_text)

    progress.phase("preparing worktree")
    worktree.add(wt_path, branch, base)

    adapter = backend_mod.adapter_path(backend_name)
    timeout_min = _timeout_min(cfg, unit)
    # run_backend narrates "agent running (<reference>)" itself, post-spawn —
    # the runner-provided reference (#126) does not exist before the handle.
    result = backend_mod.run_backend(
        cfg,
        root,
        selection.runner,
        adapter,
        unit_number=unit.number,
        cwd=wt_path,
        unit_run_dir=run_dir,
        prompt_file=prompt_file,
        timeout_min=timeout_min,
        gate_cmds=gate_cmds,
        reporter=progress,
    )
    return _conclude(
        gh,
        cfg,
        root,
        selection,
        unit,
        wt_path,
        run_dir,
        result,
        branch=branch,
        base=base,
        recorded_hash=recorded_hash,
        base_sha=base_sha,
        timeout_min=timeout_min,
        mode=mode,
        progress=progress,
    )


def _reattach_unit(
    gh: GitHub,
    cfg: Config,
    root: Path,
    selection: Selection,
    unit: Unit,
    wt_path: Path,
    run_dir: Path,
    handle: runner_mod.Handle,
    *,
    base: str,
    mode: str | None,
    progress: progress_mod.UnitProgress,
) -> Outcome:
    """A worktree plus a persisted handle: adopt the unit instead of
    redispatching (#22). Live → wait for it; dead → read the recorded
    status; either way the normal post-agent pipeline decides."""
    meta = _read_dispatch_meta(run_dir)
    if not all(meta.get(k) for k in ("spec_hash", "base_sha", "branch")):
        backend_mod.write_resume_state(
            run_dir, wt_path, "reattach impossible: dispatch metadata missing"
        )
        selection.runner.preserve(handle)
        return Outcome(
            unit,
            "failed",
            detail=(
                "worktree and handle exist but dispatch metadata is missing — "
                "not redispatching; run foreman retry after triage"
            ),
        )
    timeout_min = _timeout_min(cfg, unit)
    progress.start(meta["branch"], run_dir / "agent.log")
    runner = selection.runner
    timed_out = False
    try:
        status = runner.wait(handle, 0)
        progress.phase("reattached — unit already exited")
    except WaitTimeout:
        # Honor the ORIGINAL deadline across reattachment: a live agent gets
        # only the time remaining from its first dispatch, so repeated
        # Foreman crashes cannot extend a unit's run indefinitely (#22).
        remaining_s = backend_mod.remaining_timeout_s(run_dir, timeout_min * 60)
        progress.phase(
            f"reattached to live unit ({runner.reference(handle)}); "
            f"{remaining_s // 60}m left on the original deadline"
        )
        try:
            if remaining_s <= 0:
                raise WaitTimeout("original deadline already elapsed")
            # Wrap the adopted-unit wait the same way run_backend does, so a
            # reattached live agent is not silent for up to the full remaining
            # deadline (#83).
            status = progress_mod.wait_with_heartbeat(
                lambda s: runner.wait(handle, s),
                remaining_s,
                lambda elapsed: progress.heartbeat(elapsed, remaining_s),
                now=progress.now,
            )
        except WaitTimeout:
            timed_out = True
            runner.kill(handle)
            status = runner.wait(handle, backend_mod.KILL_REAP_S)
        finally:
            progress.settle()
    result = backend_mod.result_from_wait(run_dir, status, timed_out=timed_out)
    return _conclude(
        gh,
        cfg,
        root,
        selection,
        unit,
        wt_path,
        run_dir,
        result,
        branch=meta["branch"],
        base=base,
        recorded_hash=meta["spec_hash"],
        base_sha=meta["base_sha"],
        timeout_min=timeout_min,
        mode=mode,
        progress=progress,
    )


def _conclude(
    gh: GitHub,
    cfg: Config,
    root: Path,
    selection: Selection,
    unit: Unit,
    wt_path: Path,
    run_dir: Path,
    result: backend_mod.BackendResult,
    *,
    branch: str,
    base: str,
    recorded_hash: str,
    base_sha: str,
    timeout_min: int,
    mode: str | None,
    progress: progress_mod.UnitProgress,
) -> Outcome:
    handle = runner_mod.load_handle(cfg, root, unit.number)
    runner = selection.runner
    # Close any spinner left open by the reattach path; run_backend already
    # settled its own. Idempotent, so this is harmless on the normal path.
    progress.settle()

    def preserved(status: str, note: str, detail: str) -> Outcome:
        # Every non-success return funnels through here, so this is the single
        # honest place to narrate a terminal outcome — and it can never announce
        # a stage (verify/push/PR) that has not run yet (#83 AC2).
        progress.terminal(status, detail)
        backend_mod.write_resume_state(run_dir, wt_path, note)
        if handle is not None:
            runner.preserve(handle)
        return Outcome(
            unit, status, branch=branch, cost_usd=result.cost_usd, detail=detail
        )

    if result.quota_wait:
        return preserved(
            "waiting",
            "backend usage window exhausted",
            "backend usage limit reached — will resume after the window resets",
        )
    if result.timed_out:
        return preserved(
            "failed",
            f"agent timed out after {timeout_min}m",
            (
                f"agent timed out after {timeout_min}m — process group "
                "terminated (daemon-level descendants may survive); worktree "
                "+ session preserved"
            ),
        )
    if result.abnormal:
        return preserved(
            "failed",
            "agent terminated abnormally (no recorded exit status)",
            (
                "agent terminated abnormally — dead with no recorded exit "
                "status (worktree + session preserved)"
            ),
        )

    contract, contract_errors = backend_mod.read_result(run_dir, wt_path)
    if contract is not None and contract.status == "blocked":
        return preserved(
            "blocked",
            "agent blocked on a question",
            (
                (contract.blocked_question or "").strip().splitlines()[0]
                if contract.blocked_question
                else "blocked without a question"
            ),
        )
    if result.returncode != 0:
        return preserved(
            "failed",
            f"agent exited {result.returncode}",
            f"agent exited {result.returncode} (worktree + session preserved)",
        )
    if contract is None:
        return preserved(
            "failed",
            "invalid result contract",
            "result contract invalid: " + "; ".join(contract_errors),
        )

    ho = selection.make_handoff(wt_path, handle)
    ho.collect()
    if ho.commits_ahead(base) == 0:
        return preserved(
            "failed", "agent made no commits", "agent completed but made no commits"
        )
    if not ho.is_clean():
        return preserved(
            "failed",
            "uncommitted changes left in worktree",
            "agent left uncommitted changes in the worktree",
        )
    workflow_touched = ho.workflow_paths(base)
    if workflow_touched:
        return preserved(
            "failed",
            "workflow-touching diff (human-only)",
            handoff_mod.WORKFLOW_HUMAN_ONLY
            + " Touched: "
            + ", ".join(workflow_touched),
        )

    gate_cmds = gate.compose(cfg, runner.capabilities())
    progress.phase("verifying: " + " && ".join(" ".join(c) for c in gate_cmds))
    ok, verify_tail, failed_cmd = verify.run_gate(gate_cmds, wt_path, run_dir)
    if not ok:
        failed_display = " ".join(failed_cmd or gate_cmds[0])
        # Route the failure through the signature catalog BEFORE any resume
        # state (#18): an environmental failure must never be handed back to
        # an agent as a code bug to "fix" by weakening code.
        sig = signatures_mod.match(verify_tail, signatures_mod.load())
        if sig is not None and sig.action == "environment":
            return preserved(
                "failed",
                f"environmental failure '{sig.name}' during verify — fix the "
                f"environment, not the code.\n\nfull log: {run_dir / 'verify.log'}",
                f"environmental failure '{sig.name}' during verify "
                f"({failed_display}) — needs a human, not an agent",
            )
        return preserved(
            "failed",
            "verification failed ("
            + failed_display
            + f")\n\nfull log: {run_dir / 'verify.log'}\n\n"
            + verify_tail,
            f"verification failed ({failed_display})",
        )

    # D4/D13 re-evaluated at dispatch: visibility, access, authorship, or
    # arming drift between plan and push fails closed (TOCTOU).
    repo_now = trust_mod.repo_trust(gh, cfg)
    trust_now = trust_mod.classify_unit(gh, cfg, unit, mode or "labels")
    drift = list(trust_now.refusals)
    cap_message = selection.refusal(trust_mod.required_for(cfg, repo_now, trust_now))
    if cap_message:
        drift.append(cap_message)
    if drift:
        return preserved(
            "refused",
            "trust re-validation failed at dispatch: " + "; ".join(drift),
            "not pushed — trust drift (fail closed): " + "; ".join(drift),
        )

    fresh = pr_mod.freshness_check(
        gh, cfg, unit, recorded_hash=recorded_hash, branch=branch, mode=mode
    )
    if not fresh.ok:
        return preserved(
            "stale",
            "freshness gate: " + "; ".join(fresh.problems),
            "not pushed — " + "; ".join(fresh.problems),
        )

    spec_info = spec.validate(unit)
    human_tasks = spec.human_only_tasks(unit, spec_info)
    remote_name = worktree.remote(cfg)
    progress.phase(f"pushing {branch}")
    ho.push(remote_name, branch, first=True)
    progress.phase("opening PR")
    title = pr_mod.pr_title(cfg, unit, contract)
    body = pr_mod.pr_body(
        cfg,
        unit,
        contract,
        human_tasks=human_tasks,
        spec_hash_hex=recorded_hash,
        base_sha=base_sha,
        verify_display=gate.describe(gate_cmds),
    )
    url = pr_mod.open_pr(
        gh, cfg, unit, title=title, body=body, branch=branch, base=gh.default_branch()
    )
    progress.terminal("pr-open", url)
    return Outcome(
        unit,
        "pr-open",
        branch=branch,
        pr_url=url,
        cost_usd=result.cost_usd,
        detail=title,
    )


def _post_status(gh: GitHub, unit: Unit, outcome: Outcome) -> None:
    spec_info = spec.validate(unit)
    status = report.UnitStatus(
        unit=unit,
        state=outcome.status,
        branch=outcome.branch,
        pr_url=outcome.pr_url,
        blockers=[outcome.detail] if outcome.status in ("failed", "stale") else [],
        human_tasks=(
            spec.human_only_tasks(unit, spec_info) if not spec_info.errors else []
        ),
        blocked_question=outcome.detail if outcome.status == "blocked" else "",
    )
    report.update_status_comment(gh, status, event=_conclusion_event(outcome))


def _conclusion_event(outcome: Outcome) -> str | None:
    """The past fact this conclusion records, if any. Non-terminal outcomes
    (waiting/skipped/held/…) refresh the snapshot without logging an event."""
    if outcome.status == "pr-open":
        return f"PR opened: {outcome.pr_url}"
    if outcome.status in ("failed", "stale"):
        return f"failed: {outcome.detail}"
    if outcome.status == "blocked":
        return "blocked — awaiting human answer"
    if outcome.status == "refused":
        return f"refused: {outcome.detail}"
    return None


def run_dispatch(
    gh: GitHub,
    cfg: Config,
    root: Path,
    selection: Selection,
    target: Target,
    *,
    max_parallel: int | None = None,
) -> list[Outcome]:
    ready, outcomes = eligibility(gh, cfg, target, selection)
    if ready:
        info(
            f"dispatching {len(ready)} unit(s): {', '.join('#' + str(u.number) for u in ready)}"
        )
    workers = max(1, max_parallel or cfg.max_parallel)
    # One reporter owns stdout for the whole fan-out; it decides spinner vs
    # plain per-unit lines from `workers` (spinner only when a single worker
    # owns a TTY). Each unit narrates under its own #N: prefix (#83).
    reporter = progress_mod.DispatchReporter(workers=workers)
    if ready:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    dispatch_unit,
                    gh,
                    cfg,
                    root,
                    selection,
                    unit,
                    mode=target.mode,
                    progress=reporter.unit(unit.number),
                ): unit
                for unit in ready
            }
            for future, unit in futures.items():
                try:
                    outcomes.append(future.result())
                except ForemanError as exc:
                    reporter.unit(unit.number).terminal("failed", str(exc))
                    outcomes.append(Outcome(unit, "failed", detail=str(exc)))
    outcomes.sort(key=lambda o: o.unit.number)
    return outcomes
