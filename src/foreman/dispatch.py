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
from foreman import handoff as handoff_mod
from foreman import pr as pr_mod
from foreman import report, spec, verify, worktree
from foreman import runner as runner_mod
from foreman.config import Config
from foreman.github import GitHub
from foreman.graph import Target, Unit, dependency_satisfied
from foreman.runner import Selection, WaitTimeout
from foreman.util import ForemanError, info, write_text

RETRIGGER_SUBJECT = "chore: retrigger ci (foreman)"


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
    gh: GitHub, cfg: Config, target: Target
) -> tuple[list[Unit], list[Outcome]]:
    """Split units into ready-to-dispatch and skipped (with reasons)."""
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
) -> Outcome:
    started = time.monotonic()
    lock = runner_mod.UnitLock(cfg, root, unit.number)
    if not lock.acquire():
        return Outcome(
            unit,
            "skipped",
            detail="unit lock held — another foreman process is driving this unit",
        )
    try:
        outcome = _dispatch_locked(gh, cfg, root, selection, unit, mode=mode)
    finally:
        lock.release()
    outcome.duration_s = time.monotonic() - started
    _post_status(gh, unit, outcome)
    if outcome.status == "pr-open":
        _forget_unit(cfg, root, selection, unit)
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
            )
        return Outcome(
            unit,
            "skipped",
            detail=f"worktree already exists ({wt_path}) — run foreman retry or cleanup",
        )

    existing = worktree.attempt_branches(cfg, remote_name, unit.number)
    branch = worktree.next_attempt_branch(worktree.branch_name(cfg, unit), existing)

    comments, excluded = spec.trusted_comments(gh, cfg, unit.number)
    recorded_hash = spec.spec_hash(unit, comments)
    base_sha = worktree.base_sha(remote_name, default_branch)
    _write_dispatch_meta(
        run_dir, recorded_hash=recorded_hash, base_sha=base_sha, branch=branch
    )

    handoffs = spec.collect_handoffs(gh, cfg, unit)
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
    )
    prompt_file = run_dir / "prompt.md"
    write_text(prompt_file, prompt_text)

    worktree.add(wt_path, branch, base)

    inp = unit.inputs
    backend_name = inp.backend if inp and inp.backend else cfg.backend
    adapter = backend_mod.adapter_path(backend_name)
    timeout_min = _timeout_min(cfg, unit)
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
    runner = selection.runner
    timed_out = False
    try:
        status = runner.wait(handle, 0)
        info(f"#{unit.number}: reattached — unit already exited")
    except WaitTimeout:
        info(f"#{unit.number}: reattached to live unit; waiting up to {timeout_min}m")
        try:
            status = runner.wait(handle, timeout_min * 60)
        except WaitTimeout:
            timed_out = True
            runner.kill(handle)
            status = runner.wait(handle, backend_mod.KILL_REAP_S)
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
) -> Outcome:
    handle = runner_mod.load_handle(cfg, root, unit.number)
    runner = selection.runner

    def preserved(status: str, note: str, detail: str) -> Outcome:
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

    ok, verify_tail = verify.run_verify(cfg, wt_path, run_dir)
    if not ok:
        return preserved(
            "failed",
            "verification failed:\n\n" + verify_tail,
            f"verification failed ({' '.join(cfg.verify_command)})",
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
    ho.push(remote_name, branch, first=True)
    title = pr_mod.pr_title(cfg, unit, contract)
    body = pr_mod.pr_body(
        cfg,
        unit,
        contract,
        human_tasks=human_tasks,
        spec_hash_hex=recorded_hash,
        base_sha=base_sha,
    )
    url = pr_mod.open_pr(
        gh, cfg, unit, title=title, body=body, branch=branch, base=gh.default_branch()
    )
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
    report.update_status_comment(gh, status)


def run_dispatch(
    gh: GitHub,
    cfg: Config,
    root: Path,
    selection: Selection,
    target: Target,
    *,
    max_parallel: int | None = None,
) -> list[Outcome]:
    ready, outcomes = eligibility(gh, cfg, target)
    if ready:
        info(
            f"dispatching {len(ready)} unit(s): {', '.join('#' + str(u.number) for u in ready)}"
        )
    workers = max(1, max_parallel or cfg.max_parallel)
    if ready:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    dispatch_unit, gh, cfg, root, selection, unit, mode=target.mode
                ): unit
                for unit in ready
            }
            for future, unit in futures.items():
                try:
                    outcomes.append(future.result())
                except ForemanError as exc:
                    outcomes.append(Outcome(unit, "failed", detail=str(exc)))
    outcomes.sort(key=lambda o: o.unit.number)
    return outcomes
