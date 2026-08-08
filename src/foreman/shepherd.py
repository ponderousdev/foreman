"""Shepherd: keep open foreman PRs healthy until a human merges them.

Deterministic triggers → bounded agent actions:
  red CI      → classify by signature first (environmental: retry once via
                empty commit, then human queue; quota_wait: idle; otherwise
                mechanical: resume the agent with the failing excerpt)
  behind/dirty→ merge-tree dry-run; clean rebases are mechanical, conflicted
                ones go to the agent (rebase additively, re-verify, push)
  unresolved review-bot threads → resume the agent to adjudicate each finding
                (apply or decline-with-reasoning; blanket-accepting prohibited)
  green ∧ adjudicated ∧ mergeable ∧ not behind → label ready-for-review and
                report a dependency-aware suggested review order.

Foreman never merges. `gh run rerun` is assumed unavailable to the bot token;
the CI retrigger primitive is an empty commit.
"""

from __future__ import annotations

import graphlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from foreman import backend as backend_mod
from foreman import gate, gitops, report, reviewer, spec, verify, worktree
from foreman import signatures as signatures_mod
from foreman import trust as trust_mod
from foreman.config import Config
from foreman.dispatch import RETRIGGER_SUBJECT
from foreman.github import (
    DISPATCHED_LABELS,
    READY_FOR_REVIEW_LABEL,
    READY_FOR_REVIEW_LABELS,
    READY_HEAD_PREFIX,
    GitHub,
)
from foreman.graph import MARKER_RE
from foreman.runner import Selection
from foreman.util import info, relation_refs, warn, write_text

MAX_AGENT_ACTIONS_PER_PR = 2  # per shepherd run; watch ticks give more rounds


@dataclass
class PrWork:
    number: int
    unit_number: int
    branch: str
    url: str
    title: str
    state: str = "healthy"  # healthy | fixed | rebased | adjudicated | waiting | escalated | settling | ready
    detail: str = ""
    actions: int = 0
    cost_usd: float = 0.0


@dataclass
class ShepherdReport:
    worked: list[PrWork] = field(default_factory=list)
    ready_order: list[tuple[int, str]] = field(default_factory=list)
    environmental: dict[int, str] = field(default_factory=dict)
    waiting: dict[int, str] = field(default_factory=dict)
    cost_usd: float = 0.0


def open_foreman_prs(gh: GitHub) -> list[dict]:
    """Open PRs carrying current or transition-era Foreman provenance.

    Query each label explicitly so active PRs remain discoverable throughout
    the compatibility period. A PR carrying both names is returned once.
    """
    prs_by_number: dict[int, dict] = {}
    for label in DISPATCHED_LABELS:
        for pr in gh.prs(label=label, state="open"):
            match = MARKER_RE.search(pr.get("body") or "")
            if match:
                pr["_unit"] = int(match.group("number"))
                prs_by_number.setdefault(pr["number"], pr)
    return list(prs_by_number.values())


def classify_checks(rollup: list[dict] | None) -> tuple[str, list[dict]]:
    """(green|red|pending, failed contexts) from a statusCheckRollup list."""
    failed: list[dict] = []
    pending = False
    for ctx in rollup or []:
        status = (ctx.get("status") or "").upper()
        conclusion = (ctx.get("conclusion") or ctx.get("state") or "").upper()
        if conclusion in (
            "FAILURE",
            "TIMED_OUT",
            "ACTION_REQUIRED",
            "CANCELLED",
            "ERROR",
        ):
            failed.append(ctx)
        elif status in ("QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED") or (
            not conclusion and status not in ("COMPLETED",)
        ):
            pending = True
    if failed:
        return "red", failed
    if pending:
        return "pending", []
    return "green", []


def automation_ready_now(
    status: dict,
    review_threads: Callable[[], list[dict]],
    reviewer_gate: Callable[[], reviewer.ReviewerVerdict] | None = None,
) -> bool:
    """One current-snapshot gate for automated readiness.

    This deliberately excludes the readiness label and draft/published state:
    shepherd uses it to decide whether to write the label, while status adds
    those display-only preconditions before trusting the same live verdict.
    """
    checks_state, _failed = classify_checks(status.get("statusCheckRollup"))
    merge_state = (status.get("mergeStateStatus") or "").upper()
    mergeable = (status.get("mergeable") or "").upper()
    # GitHub uses DRAFT for the workbench before promotion and BLOCKED for an
    # otherwise mergeable PR awaiting required human approval. Every transient
    # or stale state (UNKNOWN, UNSTABLE, BEHIND, DIRTY) remains not-ready even
    # when `mergeable` happens to be MERGEABLE.
    structurally_ready = merge_state == "CLEAN" or (
        merge_state in ("DRAFT", "BLOCKED") and mergeable == "MERGEABLE"
    )
    return (
        (status.get("state") or "OPEN").upper() == "OPEN"
        and checks_state == "green"
        and structurally_ready
        and not any(not thread.get("isResolved") for thread in review_threads())
        and (reviewer_gate is None or reviewer_gate().ready)
    )


def reviewer_verdict_now(gh: GitHub, status: dict) -> reviewer.ReviewerVerdict:
    """Read and classify configured reviewer evidence for this exact head."""
    if not gh.cfg.reviewer_login:
        return reviewer.ReviewerVerdict("disabled", "external reviewer gate disabled")
    return reviewer.reviewer_verdict(
        gh.cfg,
        status.get("headRefOid") or "",
        gh.reviewer_evidence(status["number"]),
    )


def _automation_ready_for_gh(
    gh: GitHub, status: dict, review_threads: Callable[[], list[dict]]
) -> bool:
    return automation_ready_now(
        status,
        review_threads,
        lambda: reviewer_verdict_now(gh, status),
    )


def ready_for_review_now(gh: GitHub, status: dict) -> bool:
    """Revalidate a displayed-ready PR; its label is only a hint."""
    labels = {label["name"] for label in status.get("labels") or []}
    current_label_bound = READY_FOR_REVIEW_LABEL not in labels or _ready_head(
        status.get("body") or ""
    ) == status.get("headRefOid")
    return (
        not labels.isdisjoint(READY_FOR_REVIEW_LABELS)
        and current_label_bound
        and not status.get("isDraft")
        and _automation_ready_for_gh(
            gh, status, lambda: gh.review_threads(status["number"])
        )
    )


def _return_to_draft(gh: GitHub, status: dict) -> None:
    """Fail closed after readiness invalidation; legacy labels stay read-only."""
    labels = {label["name"] for label in status.get("labels") or []}
    try:
        gh.draft_own_pr(status["number"])
    except Exception as draft_error:
        if READY_FOR_REVIEW_LABEL in labels:
            try:
                gh.label_own_pr(status["number"], remove=[READY_FOR_REVIEW_LABEL])
            except Exception as label_error:
                draft_error.add_note(
                    f"readiness label cleanup also failed: {label_error}"
                )
        raise
    if READY_FOR_REVIEW_LABEL in labels:
        gh.label_own_pr(status["number"], remove=[READY_FOR_REVIEW_LABEL])


def _ready_head(body: str) -> str:
    for line in body.splitlines():
        if line.startswith(READY_HEAD_PREFIX) and line.endswith(" -->"):
            return line[len(READY_HEAD_PREFIX) : -4].strip()
    return ""


def _demote_promoted_if_invalid(gh: GitHub, status: dict) -> bool:
    """A PR Foreman promoted must return to draft when evidence regresses."""
    labels = {label["name"] for label in status.get("labels") or []}
    if READY_FOR_REVIEW_LABEL not in labels:
        return False
    try:
        head = status.get("headRefOid") or ""
        live_ready = bool(head) and _automation_ready_for_gh(
            gh, status, lambda: gh.review_threads(status["number"])
        )
        recorded_head = _ready_head(status.get("body") or "")
        if not recorded_head and not status.get("isDraft") and live_ready:
            if gh.record_ready_head_own_pr(status["number"], expected_head_oid=head):
                return False
        ready = recorded_head == head and live_ready
    except Exception:
        _return_to_draft(gh, status)
        raise
    if not ready:
        _return_to_draft(gh, status)
        return True
    return False


def _recover_interrupted_promotion(gh: GitHub, status: dict) -> bool:
    """Fail closed when a process died after publishing but before labeling."""
    labels = {label["name"] for label in status.get("labels") or []}
    recorded_head = _ready_head(status.get("body") or "")
    if READY_FOR_REVIEW_LABEL in labels or status.get("isDraft") or not recorded_head:
        return False
    head = status.get("headRefOid") or ""
    try:
        if recorded_head == head and _automation_ready_for_gh(
            gh, status, lambda: gh.review_threads(status["number"])
        ):
            return False
    except Exception:
        gh.draft_own_pr(status["number"])
        raise
    gh.draft_own_pr(status["number"])
    return True


def _promote_ready_head(gh: GitHub, status: dict) -> tuple[bool, str, dict | None]:
    """Revalidate, promote, then verify the exact head survived the mutation.

    The final status is returned on failure so a merge state hidden by GitHub's
    ``DRAFT`` state can be routed through the normal repair path immediately.
    """
    expected_head = status.get("headRefOid") or ""
    started_draft = bool(status.get("isDraft"))
    had_ready_label = any(
        label.get("name") == READY_FOR_REVIEW_LABEL
        for label in status.get("labels") or []
    )
    if not expected_head:
        return False, "head is indeterminate — keeping PR in draft", None

    fresh = gh.pr_status(status["number"])
    fresh_head = fresh.get("headRefOid") or ""
    if fresh_head != expected_head:
        if had_ready_label or (started_draft and not fresh.get("isDraft")):
            _return_to_draft(gh, fresh)
        return False, "head changed before promotion — keeping PR in draft", fresh

    fresh_threads: list[dict] | None = None

    def load_fresh_threads() -> list[dict]:
        nonlocal fresh_threads
        if fresh_threads is None:
            fresh_threads = gh.review_threads(status["number"])
        return fresh_threads

    if not _automation_ready_for_gh(gh, fresh, load_fresh_threads):
        if had_ready_label or (started_draft and not fresh.get("isDraft")):
            _return_to_draft(gh, fresh)
        return (
            False,
            "readiness changed before promotion — keeping PR in draft",
            fresh,
        )

    # Persist intent while the PR is still draft. If the process dies after
    # the ready mutation, the next tick can distinguish the interrupted
    # transition from an existing compatibility-era non-draft PR.
    if not gh.record_ready_head_own_pr(status["number"], expected_head_oid=fresh_head):
        return False, "head changed before promotion intent was recorded", None

    # Even an already-published compatibility PR crosses the guarded seam:
    # promote_own_pr becomes a no-op only after checking the exact head.
    try:
        guarded, promoted = gh.promote_own_pr(
            status["number"], expected_head_oid=fresh_head
        )
    except Exception:
        raise
    if not guarded:
        return False, "head changed during readiness guard", None
    must_return_to_draft = promoted or had_ready_label

    # GitHub has no expected-head compare-and-swap on the ready mutation.
    # Re-read immediately and undo a promotion if the remaining race window
    # admitted a push or indeterminate readiness evidence.
    try:
        after = gh.pr_status(status["number"])
        after_head = after.get("headRefOid") or ""
    except Exception:
        if must_return_to_draft:
            _return_to_draft(gh, status)
        raise
    if after.get("isDraft") or after_head != fresh_head:
        if must_return_to_draft:
            _return_to_draft(gh, after)
            detail = "readiness changed during promotion — returned PR to draft"
        else:
            detail = "readiness changed before labeling compatibility PR"
        return False, detail, after
    try:
        still_ready = _automation_ready_for_gh(
            gh, after, lambda: gh.review_threads(status["number"])
        )
    except Exception:
        if must_return_to_draft:
            _return_to_draft(gh, after)
        raise
    if not still_ready:
        if must_return_to_draft:
            _return_to_draft(gh, after)
            detail = "readiness changed during promotion — returned PR to draft"
        else:
            detail = "readiness changed before labeling compatibility PR"
        return False, detail, after

    try:
        if not gh.record_ready_head_own_pr(
            status["number"], expected_head_oid=after_head
        ):
            if must_return_to_draft:
                _return_to_draft(gh, after)
            return False, "head changed before readiness writes", None
        gh.label_own_pr(status["number"], add=[READY_FOR_REVIEW_LABEL])
    except Exception:
        if must_return_to_draft:
            _return_to_draft(gh, after)
        raise
    try:
        final = gh.pr_status(status["number"])
        final_ready = (
            not final.get("isDraft")
            and final.get("headRefOid") == after_head
            and _ready_head(final.get("body") or "") == after_head
            and _automation_ready_for_gh(
                gh, final, lambda: gh.review_threads(status["number"])
            )
        )
    except Exception:
        gh.draft_own_pr(status["number"])
        gh.label_own_pr(status["number"], remove=[READY_FOR_REVIEW_LABEL])
        raise
    if not final_ready:
        _return_to_draft(gh, final)
        return (
            False,
            "readiness changed during final writes — returned PR to draft",
            final,
        )
    if promoted:
        return True, "automation complete — promoted for human review", after
    return True, "automation complete — ready for human review", after


def _failure_text(gh: GitHub, failed: list[dict]) -> str:
    parts = []
    for ctx in failed[:5]:
        name = ctx.get("name") or ctx.get("context") or "check"
        parts.append(f"### Failing check: {name}")
        url = ctx.get("detailsUrl") or ctx.get("targetUrl") or ""
        if "/actions/runs/" in url:
            log = gh.run_log_failed(url)
            if log:
                parts.append("```text\n" + log[-8000:] + "\n```")
        elif url:
            parts.append(f"(external check: {url})")
    return "\n\n".join(parts)


def _ensure_worktree(
    cfg: Config, root: Path, unit_number: int, branch: str, remote_name: str
) -> Path:
    path = root / cfg.worktrees_dir / f"pr-{unit_number}"
    if not path.exists():
        worktree.fetch(remote_name)
        local = worktree.attempt_branches(cfg, remote_name, unit_number)
        if branch in local:
            try:
                worktree.add_existing_branch(path, branch)
            except Exception:
                worktree.add(path, branch, f"{remote_name}/{branch}")
        else:
            worktree.add(path, branch, f"{remote_name}/{branch}")
    return path


def _resume_agent(
    gh: GitHub,
    cfg: Config,
    root: Path,
    selection: Selection,
    work: PrWork,
    prompt_name: str,
    tokens: dict[str, str],
) -> backend_mod.BackendResult:
    run_dir = backend_mod.unit_dir(cfg, root, work.unit_number)
    prompt = spec.load_prompt(prompt_name, tokens)
    prompt_file = run_dir / f"{prompt_name}.md"
    write_text(prompt_file, prompt)
    adapter = backend_mod.adapter_path(cfg.backend)
    caps = backend_mod.capabilities(adapter)
    session_file = run_dir / "session"
    resume_ref = None
    if "resume" in caps and session_file.exists():
        for line in session_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("SESSION_REF="):
                resume_ref = line.split("=", 1)[1].strip() or None
                break
    wt_path = _ensure_worktree(
        cfg, root, work.unit_number, work.branch, worktree.remote(cfg)
    )
    if resume_ref is None:
        # No session to resume: prepend the deterministic resume-state.
        state_path = backend_mod.write_resume_state(
            run_dir, wt_path, "fresh shepherd invocation"
        )
        prompt = state_path.read_text(encoding="utf-8") + "\n\n---\n\n" + prompt
        write_text(prompt_file, prompt)
    return backend_mod.run_backend(
        cfg,
        root,
        selection.runner,
        adapter,
        unit_number=work.unit_number,
        cwd=wt_path,
        unit_run_dir=run_dir,
        prompt_file=prompt_file,
        timeout_min=cfg.shepherd_timeout_min,
        resume_ref=resume_ref,
    )


def _origin_refusal(
    gh: GitHub, cfg: Config, selection: Selection, unit_number: int
) -> str | None:
    """#46/D13: a fix unit inherits its branch's classification. Before any
    agent resumes on this PR's branch — CI fix, conflicted rebase, or
    adjudication — require the capabilities the origin unit's content
    requires. Consumed via selection.refusal (capabilities, never a runner
    name), exactly like eligibility does for the original dispatch.

    Classification is re-derived from current GitHub + config state on
    every tick, deliberately (ADR 0002: stored inputs, derived state — a
    stored classification can lie after a crash; re-derived state cannot).
    A later trusted_actors or visibility change is a reviewed, committed
    human attestation — the same authority as a trusted re-arm under D13 —
    so it legitimately reclassifies the origin; the unsafe direction
    (trusted → untrusted drift) tightens automatically for the same
    reason."""
    origin = trust_mod.classify_branch_origin(gh, cfg, unit_number)
    required = trust_mod.required_for(cfg, trust_mod.repo_trust(gh, cfg), origin)
    return selection.refusal(required)


def _disposition_marker(thread_id: str) -> str:
    """Invisible-in-render marker appended to every disposition reply (the
    upsert_status_comment pattern). Dedupe keys on it, not on note text: a
    retry tick re-runs the agent, which may word the note differently."""
    return f"<!-- foreman:disposition:{thread_id} -->"


def _disposition_reply_posted(thread: dict, thread_id: str, viewer: str) -> bool:
    """True when foreman already posted a disposition reply on this thread —
    the reply half of reply-then-resolve is retried by later ticks, and a
    duplicate reply would read as spam. Best-effort: the thread query
    fetches the first 50 comments, which fails toward one duplicate note,
    never toward a missed resolution."""
    marker = _disposition_marker(thread_id)
    for comment in (thread.get("comments") or {}).get("nodes") or []:
        author = (comment.get("author") or {}).get("login") or ""
        if author == viewer and marker in (comment.get("body") or ""):
            return True
    return False


def _thread_trusted(gh: GitHub, cfg: Config, thread: dict) -> bool:
    """A review thread is trusted-authored only if EVERY commenter in it is
    a trusted actor (or foreman itself). One untrusted voice taints the
    thread — the shepherd must not read any of it into a prompt (#46).

    The query fetches the first 50 comments; if the thread holds more,
    "every commenter" cannot be attested from one page — fail closed
    (same rule as the ≥100-edit content-edit reader)."""
    me = gh.viewer()
    connection = thread.get("comments") or {}
    comments = connection.get("nodes") or []
    if not comments:
        return False
    total = connection.get("totalCount")
    if isinstance(total, int) and total > len(comments):
        return False
    for comment in comments:
        author = (comment.get("author") or {}).get("login") or ""
        if author != me and author not in cfg.trusted_actors:
            return False
    return True


def _common_tokens(
    gh: GitHub, cfg: Config, selection: Selection, work: PrWork
) -> dict[str, str]:
    composed = gate.compose(cfg, selection.runner.capabilities())
    return {
        "PR_URL": work.url,
        "BRANCH": work.branch,
        "UNIT_NUMBER": str(work.unit_number),
        "DEFAULT_BRANCH": gh.default_branch(),
        "VERIFY_COMMAND": gate.describe(composed),
    }


def _repair_merge_state(
    gh: GitHub,
    cfg: Config,
    root: Path,
    selection: Selection,
    work: PrWork,
) -> PrWork:
    """Route an exposed BEHIND/DIRTY state through the standard rebase path."""
    remote_name = worktree.remote(cfg)
    wt_path = _ensure_worktree(cfg, root, work.unit_number, work.branch, remote_name)
    worktree.fetch(remote_name)
    base_ref = f"{remote_name}/{gh.default_branch()}"
    git = gitops.UnitGit(selection.runner, wt_path)
    conflicts = git.merge_tree_conflicts(base_ref)
    if not conflicts:
        if git.rebase_onto(base_ref):
            git.push(remote_name, work.branch, first=False)
            work.state, work.detail = (
                "rebased",
                "mechanical rebase onto fresh default branch",
            )
        else:
            work.state, work.detail = (
                "escalated",
                "mechanical rebase unexpectedly failed",
            )
        return work

    # A conflicted rebase resumes an agent on the branch's tree — the same
    # #46 inheritance rule as the CI fix applies.
    refusal = _origin_refusal(gh, cfg, selection, work.unit_number)
    if refusal:
        work.state, work.detail = (
            "escalated",
            "conflicted rebase on an untrusted-origin branch — an agent "
            f"is not resumed on its tree here (#46): {refusal}",
        )
        return work
    work.actions += 1
    tokens = _common_tokens(gh, cfg, selection, work)
    tokens["CONFLICTS"] = "\n".join(f"- {c}" for c in conflicts)
    result = _resume_agent(gh, cfg, root, selection, work, "shepherd-rebase", tokens)
    work.cost_usd += result.cost_usd or 0.0
    if result.ok:
        ok, _tail, _failed = verify.run_gate(
            gate.compose(cfg, selection.runner.capabilities()),
            wt_path,
            backend_mod.unit_dir(cfg, root, work.unit_number),
        )
        if ok:
            git.push(remote_name, work.branch, first=False)
            work.state, work.detail = (
                "rebased",
                f"agent resolved {len(conflicts)} conflict(s), verify green",
            )
        else:
            work.state, work.detail = "escalated", "post-rebase verification failed"
    else:
        work.state, work.detail = "escalated", "agent could not resolve the rebase"
    return work


def shepherd_pr(
    gh: GitHub,
    cfg: Config,
    root: Path,
    selection: Selection,
    pr: dict,
    catalog,
    *,
    sink: dict[int, PrWork] | None = None,
) -> PrWork:
    try:
        status = gh.pr_status(pr["number"])
    except Exception:
        # The list snapshot cannot attest checks, but it can prove that this
        # PR crossed (or interrupted) Foreman's promotion boundary. Use that
        # weaker read to fail closed when the enriched status read is unavailable.
        labels = {label["name"] for label in pr.get("labels") or []}
        interrupted = not pr.get("isDraft") and bool(_ready_head(pr.get("body") or ""))
        if READY_FOR_REVIEW_LABEL in labels or interrupted:
            _return_to_draft(gh, pr)
        raise
    work = PrWork(
        number=pr["number"],
        unit_number=pr["_unit"],
        branch=status["headRefName"],
        url=status["url"],
        title=status["title"],
    )
    if sink is not None:
        # Registered before any agent resume (#54): if post-processing
        # raises after a billable result, the caller's handler recovers
        # THIS object — accumulated cost is never replaced with a $0 stub.
        sink[work.number] = work
    remote_name = worktree.remote(cfg)
    checks_state, failed = classify_checks(status.get("statusCheckRollup"))

    # A namespaced readiness label proves this PR crossed Foreman's promotion
    # boundary. If any live predicate later regresses (push, checks, merge
    # state, or threads), return it to the draft workbench before repair.
    if _recover_interrupted_promotion(gh, status):
        work.state, work.detail = (
            "settling",
            "interrupted promotion invalidated — returned PR to draft",
        )
        return work

    if _demote_promoted_if_invalid(gh, status):
        work.state, work.detail = (
            "settling",
            "readiness evidence invalidated — returned PR to draft",
        )
        return work

    if checks_state == "pending":
        work.state, work.detail = "settling", "checks still running"
        return work

    if checks_state == "red":
        failure_text = _failure_text(gh, failed)
        sig = signatures_mod.match(failure_text, catalog)
        if sig and sig.action == "quota_wait":
            work.state, work.detail = (
                "waiting",
                f"quota signature '{sig.name}' — waiting for reset",
            )
            return work
        if sig and sig.action == "environment":
            wt_path = _ensure_worktree(
                cfg, root, work.unit_number, work.branch, remote_name
            )
            git = gitops.UnitGit(selection.runner, wt_path)
            retries = git.count_retrigger_commits(
                f"{remote_name}/{gh.default_branch()}", RETRIGGER_SUBJECT
            )
            if retries == 0:
                git.empty_commit(RETRIGGER_SUBJECT)
                git.push(remote_name, work.branch, first=False)
                work.state, work.detail = (
                    "fixed",
                    f"environmental '{sig.name}': retried once (empty commit)",
                )
            else:
                work.state = "escalated"
                work.detail = (
                    f"environmental '{sig.name}' persisted after retry — needs a human"
                )
            return work
        # Mechanical (or novel) failure → one bounded agent fix. The log
        # text of building an untrusted-origin branch is untrusted content
        # (#46): the fix agent inherits the origin unit's classification.
        refusal = _origin_refusal(gh, cfg, selection, work.unit_number)
        if refusal:
            work.state, work.detail = (
                "escalated",
                "red CI on an untrusted-origin branch — its log text and "
                f"tree are not fed to an agent here (#46): {refusal}",
            )
            return work
        work.actions += 1
        tokens = _common_tokens(gh, cfg, selection, work)
        tokens["FAILURE_EXCERPT"] = failure_text or json.dumps(failed[:3], indent=2)
        result = _resume_agent(
            gh, cfg, root, selection, work, "shepherd-ci-fix", tokens
        )
        work.cost_usd += result.cost_usd or 0.0
        work_dir = _ensure_worktree(
            cfg, root, work.unit_number, work.branch, remote_name
        )
        fix_handoff = selection.make_handoff(work_dir, None)
        if result.ok and not fix_handoff.is_clean():
            work.state, work.detail = (
                "escalated",
                "agent left uncommitted changes after CI fix",
            )
        elif result.ok:
            fix_handoff.push(remote_name, work.branch, first=False)
            work.state, work.detail = "fixed", "agent pushed a CI fix"
        else:
            work.state, work.detail = (
                "escalated",
                "agent could not fix CI (see unit log)",
            )
        if result.cost_usd:
            work.detail += f" (${result.cost_usd:.2f})"
        return work

    merge_state = (status.get("mergeStateStatus") or "").upper()
    mergeable = (status.get("mergeable") or "").upper()
    draft_conflict = (
        status.get("isDraft") and merge_state == "DRAFT" and mergeable == "CONFLICTING"
    )
    if merge_state in ("BEHIND", "DIRTY") or draft_conflict:
        return _repair_merge_state(gh, cfg, root, selection, work)

    review_threads = gh.review_threads(work.number)
    threads = [t for t in review_threads if not t.get("isResolved")]
    if threads:
        # Adjudication resumes an agent on the branch's tree — the #46
        # inheritance rule first, before any thread content is considered.
        refusal = _origin_refusal(gh, cfg, selection, work.unit_number)
        if refusal:
            work.state, work.detail = (
                "escalated",
                "unresolved threads on an untrusted-origin branch — an agent "
                f"is not resumed on its tree here (#46): {refusal}",
            )
            return work
        # Input-surface enforcement (#46): free text enters a shepherd prompt
        # only when trusted-authored. An untrusted PR-review comment (anyone
        # can comment on a public-repo PR) is dispositioned by its trusted
        # SIGNAL — thread exists, still unresolved — never by feeding its
        # body to the agent. The presence of any untrusted-authored thread on
        # a runner lacking untrusted-input escalates to a human rather than
        # letting world-writable text reach the prompt.
        untrusted_threads = [
            thread for thread in threads if not _thread_trusted(gh, cfg, thread)
        ]
        if (
            untrusted_threads
            and "untrusted-input" not in selection.runner.capabilities()
        ):
            work.actions += 1
            work.state, work.detail = (
                "escalated",
                f"{len(untrusted_threads)} review thread(s) from untrusted "
                "authors — their text is not fed to an agent on a runner "
                "without the untrusted-input boundary (#46); a human must "
                "adjudicate",
            )
            return work
        work.actions += 1
        # Only the rendered slice may be dispositioned: the disposition
        # allowlist below is built from these same threads, so an agent
        # cannot steer foreman's write token at a thread it was never shown.
        rendered_threads = threads[:20]
        rendered = []
        for thread in rendered_threads:
            comments = (thread.get("comments") or {}).get("nodes") or []
            first = comments[0] if comments else {}
            author = (first.get("author") or {}).get("login", "reviewer")
            rendered.append(
                f"- thread `{thread['id']}` on `{thread.get('path') or 'PR'}` by @{author}:\n"
                f"  > " + (first.get("body") or "").strip().replace("\n", "\n  > ")
            )
        tokens = _common_tokens(gh, cfg, selection, work)
        tokens["THREADS"] = "\n".join(rendered)
        run_dir = backend_mod.unit_dir(cfg, root, work.unit_number)
        adjudication_file = run_dir / backend_mod.ADJUDICATION_FILE
        adjudication_file.unlink(missing_ok=True)  # a fresh signal only
        tokens["ADJUDICATION_FILE"] = str(adjudication_file)
        result = _resume_agent(
            gh, cfg, root, selection, work, "shepherd-adjudicate", tokens
        )
        work.cost_usd += result.cost_usd or 0.0
        wt_path = _ensure_worktree(
            cfg, root, work.unit_number, work.branch, remote_name
        )
        adj_handoff = selection.make_handoff(wt_path, None)
        if result.ok:
            if adj_handoff.is_clean() is False:
                work.state, work.detail = (
                    "escalated",
                    "agent left uncommitted adjudication changes",
                )
                return work
            if adj_handoff.commits_ahead(f"{remote_name}/{work.branch}") > 0:
                adj_handoff.push(remote_name, work.branch, first=False)
            # The agent only RECORDS dispositions (its token is read-only,
            # #13); foreman posts each note and resolves the thread through
            # the guarded mutation seam. Thread ids are agent-controlled
            # input — only ids from the threads we rendered are accepted.
            dispositions, disp_errors = backend_mod.read_adjudication(run_dir)
            if dispositions is None:
                work.state, work.detail = (
                    "escalated",
                    "adjudication sidecar invalid — " + "; ".join(disp_errors),
                )
                return work
            by_id = {t.get("id"): t for t in rendered_threads}
            unknown = sorted(
                d.thread_id for d in dispositions if d.thread_id not in by_id
            )
            if unknown:
                work.state, work.detail = (
                    "escalated",
                    "adjudication named unknown thread id(s): " + ", ".join(unknown),
                )
                return work
            # An `applied` claim must name a commit that actually exists on
            # the branch — a resolved thread with no fix behind it would read
            # as adjudicated while the defect remains.
            git = gitops.UnitGit(selection.runner, wt_path)
            unproven = sorted(
                d.thread_id
                for d in dispositions
                if d.applied_sha and not git.commit_on_branch(d.applied_sha)
            )
            if unproven:
                work.state, work.detail = (
                    "escalated",
                    "applied disposition(s) name a commit not on the branch: "
                    + ", ".join(unproven),
                )
                return work
            me = gh.viewer()
            for disposition in dispositions:
                # Reply-then-resolve is two mutations; if resolve failed last
                # round, don't post the identical note again on retry.
                if not _disposition_reply_posted(
                    by_id[disposition.thread_id], disposition.thread_id, me
                ):
                    gh.reply_review_thread(
                        work.number,
                        disposition.thread_id,
                        disposition.note
                        + "\n\n"
                        + _disposition_marker(disposition.thread_id),
                    )
                gh.resolve_review_thread(work.number, disposition.thread_id)
            remaining = [
                t for t in gh.review_threads(work.number) if not t.get("isResolved")
            ]
            if remaining:
                work.state = "escalated"
                work.detail = f"{len(remaining)} review thread(s) still undispositioned"
            else:
                work.state, work.detail = (
                    "adjudicated",
                    f"{len(threads)} thread(s) dispositioned",
                )
        else:
            work.state, work.detail = "escalated", "adjudication agent failed"
        return work

    if _automation_ready_for_gh(gh, status, lambda: review_threads):
        ready, detail, revealed = _promote_ready_head(gh, status)
        revealed_merge_state = ((revealed or {}).get("mergeStateStatus") or "").upper()
        if not ready and revealed_merge_state in ("BEHIND", "DIRTY"):
            return _repair_merge_state(gh, cfg, root, selection, work)
        work.state, work.detail = ("ready" if ready else "settling", detail)
    elif automation_ready_now(status, lambda: review_threads):
        verdict = reviewer_verdict_now(gh, status)
        if verdict.can_request:
            attempt = verdict.next_attempt
            assert attempt is not None
            head = status.get("headRefOid") or ""
            requested = gh.request_reviewer_own_pr(
                work.number,
                expected_head_oid=head,
                body=reviewer.request_body(cfg, head, attempt),
            )
            if requested:
                work.actions += 1
                work.state, work.detail = (
                    "settling",
                    f"requested reviewer attempt {attempt} for current head",
                )
            else:
                work.state, work.detail = (
                    "settling",
                    "head changed before reviewer request",
                )
        else:
            work.state, work.detail = (
                "settling",
                f"reviewer {verdict.state}: {verdict.detail}",
            )
    else:
        work.state, work.detail = "healthy", f"mergeState={merge_state or 'UNKNOWN'}"
    return work


def merge_order(gh: GitHub, ready: list[PrWork]) -> list[tuple[int, str]]:
    """Dependency-aware suggested order among ready PRs (topo by blocked-by)."""
    by_unit = {w.unit_number: w for w in ready}
    sorter: graphlib.TopologicalSorter = graphlib.TopologicalSorter()
    for unit_number in by_unit:
        deps = [
            entry["number"]
            for entry in relation_refs(gh.issue(unit_number), "blockedBy")
            if entry["number"] in by_unit
        ]
        sorter.add(unit_number, *deps)
    ordered = list(sorter.static_order())
    return [(n, by_unit[n].url) for n in ordered]


def _own_pr(gh: GitHub, pr: dict) -> bool:
    """#82: only PRs foreman itself authored may leave provenance on the
    issue — a foreign PR wearing the label + a forged marker names an
    attacker-chosen unit number, and open_foreman_prs admits it. Provenance
    is optional display: a flaky viewer read fails toward not writing,
    never toward aborting the shepherd loop."""
    try:
        return ((pr.get("author") or {}).get("login") or "") == gh.viewer()
    except Exception:
        return False


def run_shepherd(
    gh: GitHub, cfg: Config, root: Path, selection: Selection
) -> ShepherdReport:
    out = ShepherdReport()
    catalog = signatures_mod.load()
    prs = open_foreman_prs(gh)
    if not prs:
        info("shepherd: no open foreman PRs")
        return out
    # A repository upgraded with only legacy label definitions may have no
    # new dispatch to create the namespaced definitions before shepherding an
    # in-flight PR. Ensure current definitions after compatibility discovery
    # and before any readiness write; FOREMAN_LABELS excludes legacy names.
    gh.ensure_labels()
    for pr in prs:
        sink: dict[int, PrWork] = {}
        try:
            work = shepherd_pr(gh, cfg, root, selection, pr, catalog, sink=sink)
        except Exception as exc:  # keep shepherding the rest
            warn(f"shepherd: PR #{pr['number']} failed: {exc}")
            # Recover the in-flight PrWork (#54): a raise after an agent
            # resume must not zero out the cost the resume already spent.
            work = sink.get(pr["number"]) or PrWork(
                number=pr["number"],
                unit_number=pr["_unit"],
                branch=pr.get("headRefName", ""),
                url=pr.get("url", ""),
                title=pr.get("title", ""),
            )
            work.state, work.detail = "escalated", str(exc)
        out.worked.append(work)
        # #54: shepherd spend was never accumulated — ShepherdReport.cost_usd
        # stayed 0 and watch's --budget-usd systematically undercounted.
        out.cost_usd += work.cost_usd
        if work.state == "escalated":
            out.environmental[work.unit_number] = work.detail
            # Shepherd holds no Unit to re-render a snapshot from, so it
            # only appends the fact. Dedup makes repeated ticks no-ops.
            if _own_pr(gh, pr):
                report.append_status_event(
                    gh, work.unit_number, f"escalated: {work.detail}"
                )
        if work.state == "ready" and _own_pr(gh, pr):
            report.append_status_event(
                gh, work.unit_number, "ready for review — human turn"
            )
        if work.state == "waiting":
            out.waiting[work.unit_number] = work.detail
    ready = [w for w in out.worked if w.state == "ready"]
    out.ready_order = merge_order(gh, ready)
    return out
