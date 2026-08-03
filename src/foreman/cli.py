"""Command-line interface. Taskfile targets are thin wrappers over these
subcommands: plan, vet, dispatch, shepherd, watch, status, retry,
cleanup. plan/status/vet-draft are read-only by construction
(github.GitHub.read_only) — the write contract is enforced, not promised.

Naming note (spec, "Naming"): `vet` is v1's issue-analysis command, renamed
from `preflight` during extraction so #15's security assertion gate can take
the `preflight` name without two meanings ever coexisting.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from foreman import backend as backend_mod
from foreman import dispatch as dispatch_mod
from foreman import inputs as inputs_mod
from foreman import report, spec, worktree
from foreman import runner as runner_mod
from foreman import shepherd as shepherd_mod
from foreman import watch as watch_mod
from foreman.config import Config
from foreman.config import load as load_config
from foreman.github import Gh, GitHub
from foreman.graph import (
    Target,
    _unit_from_issue,
    dependency_satisfied,
    detect_cycle,
    prepare_target,
    waves,
)
from foreman.util import ForemanError, error, info, repo_root, warn, write_text


def _add_target_args(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--milestone", help="milestone number or exact title")
    group.add_argument(
        "--issue", type=int, help="single issue number (with its sub-issues)"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="foreman",
        description="Deterministic supervisor: dispatch ready issues to headless "
        "agents, verify, open PRs, shepherd them to mergeable. Humans merge.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser(
        "plan", help="dry run: graph, waves, ready set (no side effects)"
    )
    _add_target_args(p_plan)

    p_vet = sub.add_parser(
        "vet",
        help="read-only agent analysis of the target; drafts correction comments",
    )
    _add_target_args(p_vet)
    p_vet.add_argument(
        "--post",
        action="store_true",
        help="post the human-reviewed drafted comments from .foreman/vet/comments/",
    )

    sub.add_parser(
        "preflight",
        help="empirically assert the security controls before any dispatch (#15)",
    )

    p_dispatch = sub.add_parser(
        "dispatch", help="dispatch ready units → verify → open PRs"
    )
    _add_target_args(p_dispatch)
    p_dispatch.add_argument("--max-parallel", type=int, default=None)

    sub.add_parser(
        "shepherd", help="repair CI, adjudicate reviews, rebase, report merge order"
    )

    p_watch = sub.add_parser(
        "watch", help="loop plan→dispatch→shepherd with heartbeats"
    )
    _add_target_args(p_watch)
    p_watch.add_argument("--interval", default="5m", help="tick interval (300, 5m, 1h)")
    p_watch.add_argument(
        "--budget-usd", type=float, default=None, help="aggregate stop budget"
    )

    p_status = sub.add_parser("status", help="read-only snapshot + human-action queue")
    # #84: with no target, status prints an overall snapshot across every open
    # foreman PR + local run state — so the target is optional here alone.
    _add_target_args(p_status, required=False)

    p_retry = sub.add_parser("retry", help="re-dispatch a unit whose PR a human closed")
    p_retry.add_argument("--unit", type=int, required=True)

    p_attach = sub.add_parser(
        "attach", help="resume a preserved failed unit in place (runner-polymorphic)"
    )
    p_attach.add_argument("--unit", type=int, required=True)

    sub.add_parser(
        "cleanup", help="prune worktrees + foreman branches for closed units"
    )
    return parser


def _context(read_only: bool) -> tuple[Config, Path, GitHub]:
    root = repo_root()
    cfg = load_config(root)
    gh = GitHub(Gh(), cfg)
    gh.read_only = read_only
    return cfg, root, gh


def _print_plan(
    gh: GitHub, cfg: Config, target: Target, selection: runner_mod.Selection
) -> int:
    remote_name = worktree.remote(cfg)
    advertised = selection.runner.capabilities()
    print(f"Target: {target.label}")
    print(f"Inputs: {inputs_mod.describe_mode(target.mode, cfg)}")
    print(f"Base:   {remote_name}/{gh.default_branch()}")
    print(
        f"Runner: {cfg.runner} "
        f"(capabilities: {', '.join(sorted(advertised)) or 'none'})"
    )
    if target.repo_trust is not None:
        marker = "untrusted-input" if target.repo_trust.untrusted else "trusted"
        print(f"Trust:  repo is {marker} — {target.repo_trust.reason}")
    print()

    cycle = detect_cycle(target)
    if cycle:
        error("dependency cycle: " + " -> ".join(f"#{n}" for n in cycle))
        return 1

    fail_loud = False
    refusal_message = _target_refusal(cfg, target, selection)
    if refusal_message:
        fail_loud = True
        error(f"plan refused: {refusal_message}")
        print()
    ready, skipped = dispatch_mod.eligibility(gh, cfg, target, selection)
    by_number = {o.unit.number: o for o in skipped}
    print("Waves (dependency order):")
    for index, wave in enumerate(waves(target), 1):
        print(f"  wave {index}:")
        for number in wave:
            unit = target.units[number]
            spec_info = spec.validate(unit)
            if unit in ready:
                state = "READY"
            elif not unit.open:
                state = "closed"
            else:
                outcome = by_number.get(number)
                state = outcome.status if outcome else "waiting"
            notes = []
            for dep in unit.blocked_by:
                dep_inputs = target.units[dep].inputs if dep in target.units else None
                done = dependency_satisfied(
                    gh, cfg, dep, inputs=dep_inputs, mode=target.mode
                )
                mark = "✓" if done.satisfied else "✗"
                notes.append(f"{mark}#{dep} ({done.how})")
                for note in done.warnings:
                    warn(note)
            problems = (
                unit.errors
                + (unit.inputs.errors if unit.inputs else [])
                + spec_info.errors
            )
            if problems:
                fail_loud = True
            line = f"    #{number} [{state}] {unit.title}"
            commit_type = unit.inputs.commit_type if unit.inputs else cfg.default_type
            line += f"  (type={commit_type}"
            if notes:
                line += f"; deps: {', '.join(notes)}"
            line += ")"
            print(line)
            for problem in problems:
                print(f"      ERROR: {problem}")
            for warning in spec_info.warnings + (
                unit.inputs.warnings if unit.inputs else []
            ):
                print(f"      note: {warning}")
    print()
    print(f"Ready now: {', '.join('#' + str(u.number) for u in ready) or '(none)'}")

    notices = _concurrent_activity(gh, target)
    if notices:
        print()
        print("Concurrent-activity notice (collisions possible — vet should look):")
        for notice in notices:
            print(f"  - {notice}")
    return 1 if fail_loud else 0


def _concurrent_activity(
    gh: GitHub, target: Target, *, redact_titles: bool = False
) -> list[str]:
    """`redact_titles` is for prompt consumers (#46): milestone titles are
    user-controlled free text whose provenance cannot be attested (the
    creator may have lost access; renames have no event history), so
    prompts cite milestone numbers only. Human terminal output keeps the
    titles."""
    notices = []
    for ms in gh.milestones(state="open"):
        if target.milestone and ms["title"] == target.milestone:
            continue
        if ms.get("open_issues"):
            name = f"#{ms['number']}" if redact_titles else f"'{ms['title']}'"
            notices.append(
                f"open milestone {name} with {ms['open_issues']} open issue(s)"
            )
    others = [
        p
        for p in gh.prs(state="open")
        if "foreman-dispatched"
        not in [label["name"] for label in p.get("labels") or []]
    ]
    if others:
        notices.append(
            f"{len(others)} open non-foreman PR(s) may land on the default branch mid-run"
        )
    return notices


def _target_refusal(
    cfg: Config, target: Target, selection: runner_mod.Selection
) -> str | None:
    """The repo-level hard-mismatch check (#28): declared
    required_capabilities plus the D4 repo-trust injection, against what the
    configured runner advertises."""
    from foreman import trust as trust_mod

    if target.repo_trust is None:
        return None
    required = trust_mod.required_for(cfg, target.repo_trust, None)
    return selection.refusal(required)


def cmd_plan(args: argparse.Namespace) -> int:
    cfg, _root, gh = _context(read_only=True)
    selection = runner_mod.select(cfg)
    target = prepare_target(gh, cfg, milestone=args.milestone, issue=args.issue)
    return _print_plan(gh, cfg, target, selection)


def cmd_dispatch(args: argparse.Namespace) -> int:
    cfg, root, gh = _context(read_only=False)
    backend_mod.assert_backend_version(cfg)
    selection = runner_mod.select(cfg)
    target = prepare_target(gh, cfg, milestone=args.milestone, issue=args.issue)
    cycle = detect_cycle(target)
    if cycle:
        error(
            "refusing to dispatch: dependency cycle "
            + " -> ".join(f"#{n}" for n in cycle)
        )
        return 1
    refusal_message = _target_refusal(cfg, target, selection)
    if refusal_message:
        error(f"refusing to dispatch: {refusal_message}")
        return 1
    outcomes = dispatch_mod.run_dispatch(
        gh, cfg, root, selection, target, max_parallel=args.max_parallel
    )
    statuses = [
        report.UnitStatus(
            unit=o.unit,
            state=o.status,
            branch=o.branch,
            pr_url=o.pr_url,
            detail=o.detail,
        )
        for o in outcomes
    ]
    print()
    print(report.summary_table(statuses))
    failed = [o for o in outcomes if o.status in ("failed", "blocked", "stale")]
    for outcome in failed:
        print(f"\n#{outcome.unit.number} {outcome.status}: {outcome.detail}")
    return 1 if failed else 0


def cmd_shepherd(_args: argparse.Namespace) -> int:
    cfg, root, gh = _context(read_only=False)
    backend_mod.assert_backend_version(cfg)
    selection = runner_mod.select(cfg)
    shep = shepherd_mod.run_shepherd(gh, cfg, root, selection)
    if shep.worked:
        rows = [
            [f"#{w.unit_number}", f"PR #{w.number}", w.state, w.detail[:70]]
            for w in shep.worked
        ]
        print(report.table(["unit", "pr", "state", "detail"], rows))
    if shep.ready_order:
        print()
        print("Suggested merge order (foreman never merges):")
        for index, (number, url) in enumerate(shep.ready_order, 1):
            print(f"  {index}. #{number}  {url}")
    if shep.environmental:
        print()
        for number, detail in sorted(shep.environmental.items()):
            print(f"NEEDS HUMAN #{number}: {detail}")
    return 0


def _read_local_result(path: Path) -> dict | None:
    """Best-effort read of a unit's sidecar result.json for the overall
    snapshot (#84). Display-only, so a missing or malformed file is simply
    'nothing to show', never a hard error."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _status_overall(cfg: Config, root: Path, gh: GitHub) -> int:
    """Untargeted `foreman status` (#84): an overall snapshot with no
    --milestone/--issue — every open foreman PR and its state (in-flight
    units, derived from branches/PRs), the consolidated human-action queue,
    and recent terminal outcomes drawn from local run dirs."""
    open_prs = shepherd_mod.open_foreman_prs(gh)
    # Every open PR gets a row (a retried unit can hold several open
    # attempts at once); the unit-number set exists only so local-dir
    # entries are not double-reported.
    units_with_prs = {p["_unit"] for p in open_prs}
    statuses: list[report.UnitStatus] = []
    human_tasks: dict[int, list[str]] = {}
    blocked: dict[int, str] = {}
    ready: list[shepherd_mod.PrWork] = []

    def add_tasks(number: int, tasks: list[str]) -> None:
        # Spec [HUMAN] items and contract human_tasks overlap by design
        # (the implementer copies them) — dedupe, preserving order, and
        # never create an empty entry (human_queue treats presence as work).
        fresh = [t for t in tasks if t.strip()]
        if not fresh:
            return
        existing = human_tasks.setdefault(number, [])
        existing.extend(t for t in fresh if t not in existing)

    spec_read: set[int] = set()
    for pr in sorted(open_prs, key=lambda p: (p["_unit"], p["number"])):
        number = pr["_unit"]
        unit = _unit_from_issue(gh.issue(number))
        if number not in spec_read:
            spec_read.add(number)
            spec_info = spec.validate(unit)
            if not spec_info.errors:
                add_tasks(number, spec.human_only_tasks(unit, spec_info))
        status = gh.pr_status(pr["number"])
        checks_state, _failed = shepherd_mod.classify_checks(
            status.get("statusCheckRollup")
        )
        labels = [label["name"] for label in status.get("labels") or []]
        # A ready-to-merge label can go stale (new commits, regressed
        # checks, base advanced, fresh review threads) — readiness
        # revalidates every predicate the shepherd requires: green checks,
        # a clean merge state, and no unresolved review threads. The
        # checks state renders either way.
        ready_now = (
            "ready-to-merge" in labels
            and checks_state == "green"
            and (status.get("mergeStateStatus") or "").upper() == "CLEAN"
            and not any(
                not t.get("isResolved") for t in gh.review_threads(pr["number"])
            )
        )
        state = "ready-to-merge" if ready_now else f"pr-open ({checks_state})"
        if ready_now:
            ready.append(
                shepherd_mod.PrWork(
                    number=pr["number"],
                    unit_number=number,
                    branch=status["headRefName"],
                    url=status["url"],
                    title=status["title"],
                )
            )
        statuses.append(
            report.UnitStatus(
                unit=unit,
                state=state,
                branch=status["headRefName"],
                pr_url=status["url"],
                checks=checks_state,
                detail=status["title"],
            )
        )

    # Local run dirs supply three things. For EVERY unit with a contract:
    # agent-reported human_tasks and (open-issue) blocked questions join the
    # queue — an open PR does not erase the human work its run discovered.
    # For units WITHOUT an open PR only: an entry in the snapshot — active
    # runs (run record, no contract yet) go to the in-flight section, and
    # contracted runs to recent outcomes as agent:<status>, display-only
    # (a "completed" contract does not prove the supervisor's post-agent
    # gates passed; no PR here says otherwise).
    candidates: list[tuple[str, int, str, str]] = []
    units_root = root / cfg.runtime_dir / "units"
    for unit_dir in (d for d in units_root.glob("*") if d.name.isdigit()):
        number = int(unit_dir.name)
        started = _read_local_result(unit_dir / "run_started.json") or {}
        started_at = started.get("started_at")
        sort_key = str(started_at) if isinstance(started_at, str) else ""
        result = _read_local_result(unit_dir / "result.json")
        if result is None:
            if started and number not in units_with_prs:
                # A recorded exit status means the run is DEAD without a
                # contract — a terminal outcome, not an in-flight row. No
                # status file and no contract reads as still active
                # (best-effort: a SIGKILLed wrapper leaves no record and
                # will show as in-flight until cleaned).
                if (unit_dir / "exit-status").exists():
                    candidates.append(
                        (sort_key, number, "agent:died (no contract)", "")
                    )
                else:
                    statuses.append(
                        report.UnitStatus(
                            unit=_unit_from_issue(gh.issue(number)),
                            state="in-flight (no contract)",
                            detail=sort_key,
                        )
                    )
            continue
        # The real contract validator judges the sidecar (schema, allowed
        # statuses, required fields). The worktree feeds the BLOCKED.md
        # fallback, so resolve the unit's preserved worktree like targeted
        # status does (fall back to the run dir when none survives).
        wt = next(iter((root / cfg.worktrees_dir).glob(f"{number}-*")), unit_dir)
        contract, _errors = backend_mod.read_result(unit_dir, wt)
        if contract is None:
            if number not in units_with_prs:
                candidates.append((sort_key, number, "agent:invalid-contract", ""))
            continue
        # Queue candidates first; the issue state (one gh read) is checked
        # only when the contract could actually contribute queue entries —
        # a completed no-task contract costs no API call.
        question = contract.blocked_question
        question = question.strip() if isinstance(question, str) else ""
        contributes = bool(contract.human_tasks) or (
            contract.status == "blocked" and question
        )
        issue_open = contributes and (
            (gh.issue(number).get("state") or "").upper() == "OPEN"
        )
        # Tasks and blocked questions from closed/cancelled issues must
        # not haunt the queue forever.
        if issue_open and contract.human_tasks:
            add_tasks(number, list(contract.human_tasks))
        if contract.status == "blocked" and issue_open and question:
            blocked[number] = question
        if number in units_with_prs:
            continue  # represented by its PR row(s) above
        detail = contract.summary.strip().splitlines()[0] if contract.summary else ""
        candidates.append((sort_key, number, f"agent:{contract.status}", detail))
    # Bounded and recency-ordered: newest run_started first, cap at 10.
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    outcomes = [(n, st, d) for _ts, n, st, d in candidates[:10]]

    # merge_order is unit-keyed; a unit with several simultaneously-ready
    # attempts contributes its lowest-numbered PR (the human sees the rest
    # as rows above and resolves the duplicate-attempt state deliberately).
    first_ready: dict[int, shepherd_mod.PrWork] = {}
    for work in sorted(ready, key=lambda w: w.number):
        first_ready.setdefault(work.unit_number, work)
    print(
        report.overall_snapshot(
            statuses=statuses,
            merge_order=shepherd_mod.merge_order(gh, list(first_ready.values())),
            human_tasks=human_tasks,
            blocked=blocked,
            outcomes=outcomes,
        )
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg, root, gh = _context(read_only=True)
    if args.milestone is None and args.issue is None:
        return _status_overall(cfg, root, gh)
    target = prepare_target(gh, cfg, milestone=args.milestone, issue=args.issue)
    open_prs = shepherd_mod.open_foreman_prs(gh)
    prs_by_unit = {p["_unit"]: p for p in open_prs}
    statuses: list[report.UnitStatus] = []
    human_tasks: dict[int, list[str]] = {}
    blocked: dict[int, str] = {}
    ready: list[shepherd_mod.PrWork] = []
    for number in sorted(target.units):
        unit = target.units[number]
        spec_info = spec.validate(unit)
        tasks = spec.human_only_tasks(unit, spec_info) if not spec_info.errors else []
        if tasks and unit.open:
            human_tasks[number] = tasks
        if not unit.open:
            done = dependency_satisfied(
                gh, cfg, number, inputs=unit.inputs, mode=target.mode
            )
            statuses.append(
                report.UnitStatus(unit=unit, state="merged", detail=done.how)
            )
            continue
        pr = prs_by_unit.get(number)
        if pr:
            status = gh.pr_status(pr["number"])
            checks_state, _failed = shepherd_mod.classify_checks(
                status.get("statusCheckRollup")
            )
            labels = [label["name"] for label in status.get("labels") or []]
            state = "ready-to-merge" if "ready-to-merge" in labels else "pr-open"
            if state == "ready-to-merge":
                ready.append(
                    shepherd_mod.PrWork(
                        number=pr["number"],
                        unit_number=number,
                        branch=status["headRefName"],
                        url=status["url"],
                        title=status["title"],
                    )
                )
            statuses.append(
                report.UnitStatus(
                    unit=unit,
                    state=state,
                    branch=status["headRefName"],
                    pr_url=status["url"],
                    checks=checks_state,
                    detail=status["title"],
                )
            )
            continue
        blocked_file = root / cfg.runtime_dir / "units" / str(number) / "result.json"
        detail = ""
        if blocked_file.exists():
            contract, _errors = backend_mod.read_result(
                blocked_file.parent, worktree.worktree_path(cfg, root, unit)
            )
            if contract and contract.status == "blocked" and contract.blocked_question:
                blocked[number] = contract.blocked_question
                detail = "blocked on a question"
        outcome_state = "waiting"
        if unit.inputs and unit.inputs.hold:
            outcome_state = "held"
        elif unit.inputs and not unit.inputs.armed:
            outcome_state = "not-armed"
        statuses.append(
            report.UnitStatus(unit=unit, state=outcome_state, detail=detail)
        )
    print(report.summary_table(statuses))
    print()
    print(
        report.human_queue(
            merge_order=shepherd_mod.merge_order(gh, ready),
            human_tasks=human_tasks,
            blocked=blocked,
            environmental={},
        )
    )
    return 0


def cmd_retry(args: argparse.Namespace) -> int:
    cfg, root, gh = _context(read_only=False)
    backend_mod.assert_backend_version(cfg)
    target = prepare_target(gh, cfg, milestone=None, issue=args.unit)
    unit = target.units[args.unit]
    if not unit.open:
        error(f"#{args.unit} is closed — nothing to retry")
        return 1
    remote_name = worktree.remote(cfg)
    attempts = worktree.attempt_branches(cfg, remote_name, unit.number)
    open_prs = [p for p in gh.prs(state="open") if p["headRefName"] in attempts]
    if open_prs:
        error(
            f"#{args.unit} still has an open PR ({open_prs[0]['url']}) — close it first"
        )
        return 1
    selection = runner_mod.select(cfg)
    stale_wt = worktree.worktree_path(cfg, root, unit)
    if stale_wt.exists():
        info(f"removing preserved worktree {stale_wt}")
        worktree.remove(stale_wt)
        runner_mod.delete_handle(cfg, root, unit.number)
    outcome = dispatch_mod.dispatch_unit(
        gh, cfg, root, selection, unit, mode=target.mode
    )
    print(
        report.summary_table(
            [
                report.UnitStatus(
                    unit=unit,
                    state=outcome.status,
                    branch=outcome.branch,
                    pr_url=outcome.pr_url,
                    detail=outcome.detail,
                )
            ]
        )
    )
    return 0 if outcome.dispatched else 1


def cmd_attach(args: argparse.Namespace) -> int:
    """Local triage (#37): a dead subprocess cannot be re-entered, so local
    'attach' is 'go to where the state is and resume'. The preserved
    worktree and the Claude session under ~/.claude are what survive; this
    prints the exact resume command rather than pretending to attach to a
    process that is gone. Never appears to succeed while doing nothing."""
    cfg, root, _gh = _context(read_only=True)
    if cfg.runner != "local":
        error(
            f"attach under runner '{cfg.runner}' is not implemented in v2.0 "
            "(docker triage is v2.2 #27, sprite triage is v2.1 #32)"
        )
        return 1
    run_dir = backend_mod.unit_dir(cfg, root, args.unit)
    wt_glob = list((root / cfg.worktrees_dir).glob(f"{args.unit}-*")) + list(
        (root / cfg.worktrees_dir).glob(f"pr-{args.unit}")
    )
    session_ref = None
    session_file = run_dir / "session"
    if session_file.exists():
        for line in session_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("SESSION_REF="):
                session_ref = line.split("=", 1)[1].strip() or None
    if not wt_glob:
        error(
            f"no preserved worktree for unit #{args.unit} under "
            f"{root / cfg.worktrees_dir} — nothing to resume (was it cleaned "
            "up, or never dispatched?)"
        )
        return 1
    wt_path = wt_glob[0]
    resume_state = run_dir / "resume-state.md"
    print(f"Local triage for unit #{args.unit} (no live process to attach to):")
    print(f"  worktree:     {wt_path}")
    if resume_state.exists():
        print(f"  resume state: {resume_state}")
    print()
    if session_ref:
        print("  Resume the preserved Claude session in the worktree:")
        print(f"    cd {wt_path} && claude --resume {session_ref}")
    else:
        print(
            "  No captured session ref — start a fresh session in the "
            "worktree and hand it the resume state:"
        )
        print(f"    cd {wt_path} && claude")
        if resume_state.exists():
            print(f"    # then paste the context from {resume_state}")
    return 0


def cmd_cleanup(_args: argparse.Namespace) -> int:
    cfg, root, gh = _context(read_only=False)
    remote_name = worktree.remote(cfg)
    worktree.fetch(remote_name)
    import re as _re

    from foreman.util import run as _run

    pattern = _re.compile(rf"^{_re.escape(cfg.branch_prefix)}/[^/]+/(?P<number>\d+)-")
    by_unit: dict[int, list[str]] = {}
    refs = _run(["git", "ls-remote", "--heads", remote_name]).stdout
    local = _run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"]
    ).stdout
    names = {
        line.split("\t")[1][len("refs/heads/") :]
        for line in refs.splitlines()
        if "\t" in line
    }
    names.update(name for name in local.split() if name)
    for name in sorted(names):
        match = pattern.match(name)
        if match:
            by_unit.setdefault(int(match.group("number")), []).append(name)

    removed = 0
    for number, branches in sorted(by_unit.items()):
        issue = gh.issue(number)
        if (issue.get("state") or "").upper() != "CLOSED":
            continue
        if any(gh.prs(head=branch, state="open") for branch in branches):
            continue
        for branch in branches:
            info(f"cleanup: deleting branch {branch} (unit #{number} closed)")
            worktree.delete_branch(cfg, remote_name, branch)
            removed += 1
        for path in (root / cfg.worktrees_dir).glob(f"{number}-*"):
            worktree.remove(path)
        pr_wt = root / cfg.worktrees_dir / f"pr-{number}"
        if pr_wt.exists():
            worktree.remove(pr_wt)
    info(f"cleanup: removed {removed} branch(es); in-flight units untouched")
    return 0


def cmd_vet(args: argparse.Namespace) -> int:
    cfg, root, gh = _context(read_only=not args.post)
    comments_dir = root / cfg.runtime_dir / "vet" / "comments"
    if args.post:
        posted = 0
        for path in sorted(comments_dir.glob("*.md")):
            number = int(path.stem)
            body = path.read_text(encoding="utf-8").strip()
            if not body:
                continue
            gh.post_vet_correction(number, body, human_approved=True)
            path.rename(path.with_suffix(".posted"))
            posted += 1
            info(f"posted correction comment on #{number}")
        if not posted:
            warn(f"no draft comments found in {comments_dir}")
        return 0

    backend_mod.assert_backend_version(cfg)
    target = prepare_target(
        gh, cfg, milestone=args.milestone, issue=args.issue, classify_closed=True
    )
    selection = runner_mod.select(cfg)
    # vet's input surface (#46): issue + sub-issue titles/bodies and only
    # trusted-authored comments — untrusted comments are excluded in code,
    # exactly as the implementer's surface excludes them (#14). vet is
    # read-only and drafts comments for HUMAN approval, but the same rule
    # holds: world-writable text never enters an agent prompt un-fenced —
    # including the bodies themselves: a unit classified untrusted-input
    # (D13) is refused here via the same capability machinery as dispatch,
    # never fed to a vet agent on a runner lacking the boundary.
    bodies = []
    refused = 0
    for number in sorted(target.units):
        unit = target.units[number]
        refusal = selection.refusal(unit.required_capabilities)
        if refusal:
            refused += 1
            warn(f"vet: skipping #{number} — {refusal}")
            continue
        comments, excluded = spec.trusted_comments(gh, cfg, number)
        bodies.append(f"# Unit #{number}: {unit.title}\n\n{unit.body}")
        for sub in unit.sub_issues:
            bodies.append(
                f"## Sub-issue #{sub['number']}: {sub.get('title', '')}\n\n{sub.get('body') or ''}"
            )
        for comment in comments:
            author = (comment.get("user") or {}).get("login", "unknown")
            bodies.append(
                f"### Comment on #{number} by @{author}\n\n{comment.get('body') or ''}"
            )
        if excluded:
            bodies.append(
                f"_Note: {excluded} comment(s) from untrusted authors were "
                f"withheld from this analysis of #{number}._"
            )
    if not bodies:
        warn(
            f"vet: nothing to analyze — {refused} unit(s) refused "
            "(untrusted-classified without the untrusted-input boundary)"
            if refused
            else "vet: nothing to analyze — no units in the target"
        )
        return 1
    # Prompt-safe target reference (#46): milestone titles are free text
    # with unattestable provenance — the prompt cites the number; the units
    # themselves carry the real, classified content.
    prompt_target = (
        f"milestone #{target.milestone_number}"
        if target.milestone_number is not None
        else target.label
    )
    tokens = {
        "TARGET": prompt_target,
        "CONCURRENT": "\n".join(_concurrent_activity(gh, target, redact_titles=True))
        or "(none detected)",
        "UNITS": "\n\n---\n\n".join(bodies),
    }
    prompt = spec.load_prompt("vet", tokens)
    run_dir = root / cfg.runtime_dir / "vet"
    run_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = run_dir / "prompt.md"
    write_text(prompt_file, prompt)
    adapter = backend_mod.adapter_path(cfg.backend)
    os.environ["FOREMAN_READONLY"] = "1"
    try:
        result = backend_mod.run_backend(
            cfg,
            root,
            selection.runner,
            adapter,
            unit_number=0,  # vet is not a unit; 0 is reserved for it
            cwd=root,
            unit_run_dir=run_dir,
            prompt_file=prompt_file,
            timeout_min=cfg.shepherd_timeout_min,
        )
    finally:
        os.environ.pop("FOREMAN_READONLY", None)
    findings_src = run_dir / "adapter-stdout.log"
    findings = findings_src.read_text(encoding="utf-8") if findings_src.exists() else ""
    findings_file = run_dir / "findings.md"
    write_text(findings_file, findings)
    drafted = _extract_draft_comments(findings)
    comments_dir.mkdir(parents=True, exist_ok=True)
    for number, body in drafted.items():
        write_text(comments_dir / f"{number}.md", body)
    info(f"vet findings: {findings_file}")
    if drafted:
        info(
            f"{len(drafted)} drafted correction comment(s) in {comments_dir} — review/edit "
            "them, then run: foreman vet --post"
        )
    else:
        info("no correction comments drafted")
    return 0 if result.ok else 1


def _extract_draft_comments(findings: str) -> dict[int, str]:
    import re as _re

    drafted: dict[int, str] = {}
    parts = _re.split(r"^## DRAFT COMMENT FOR #(\d+)\s*$", findings, flags=_re.M)
    for index in range(1, len(parts) - 1, 2):
        number = int(parts[index])
        body = parts[index + 1].split("\n## ")[0].strip()
        if body:
            drafted[number] = body
    return drafted


def cmd_preflight(_args: argparse.Namespace) -> int:
    from foreman import preflight as preflight_mod

    cfg, _root, gh = _context(read_only=True)
    read_token = preflight_mod.read_token_from_env()
    probes = preflight_mod.run_preflight(
        slug=gh.repo_slug(),
        default_branch=gh.default_branch(),
        expected_login=cfg.expected_login,
        write=preflight_mod.gh_with_token(None),
        read=preflight_mod.gh_with_token(read_token),
    )
    print(preflight_mod.render(probes))
    failed = [p for p in probes if not p.ok]
    if failed:
        error(f"preflight FAILED: {len(failed)} assertion(s) did not hold")
        return 1
    info("preflight: all assertions hold")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    cfg, root, _gh = _context(read_only=False)
    backend_mod.assert_backend_version(cfg)
    selection = runner_mod.select(cfg)
    return watch_mod.run_watch(
        cfg,
        root,
        selection,
        milestone=args.milestone,
        issue=args.issue,
        interval_s=watch_mod.parse_interval(args.interval),
        budget_usd=args.budget_usd,
    )


_COMMANDS = {
    "plan": cmd_plan,
    "vet": cmd_vet,
    "preflight": cmd_preflight,
    "dispatch": cmd_dispatch,
    "shepherd": cmd_shepherd,
    "watch": cmd_watch,
    "status": cmd_status,
    "retry": cmd_retry,
    "attach": cmd_attach,
    "cleanup": cmd_cleanup,
}


def _assert_bot_devcontainer() -> None:
    """D2 startup tripwire: Foreman runs only in the bot devcontainer, whose
    profile sets FOREMAN_DEVCONTAINER=bot in containerEnv. A guard against
    accident, not intent — trivially spoofable, deliberately cheap. There is
    no bare-host mode and none may be added (D2)."""
    if os.environ.get("FOREMAN_DEVCONTAINER") != "bot":
        raise ForemanError(
            "refusing to start: FOREMAN_DEVCONTAINER != 'bot'. Foreman runs "
            "only inside the bot devcontainer (spec D2) — its HOME holds no "
            "personal credentials and its tokens are scoped. There is no "
            "bare-host mode."
        )


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        print("foreman: Python >= 3.11 required (tomllib)", file=sys.stderr)
        return 2
    args = _parser().parse_args(argv)
    try:
        _assert_bot_devcontainer()
        return _COMMANDS[args.command](args)
    except ForemanError as exc:
        error(str(exc))
        return 1
    except KeyboardInterrupt:
        error("interrupted")
        return 130
