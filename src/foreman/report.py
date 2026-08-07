"""Human-facing output: summary tables, the per-unit status comment
(single, marker-identified, edited in place, carrying a snapshot plus an
append-only event log), and the consolidated human-action queue. Display
only — the comment body is read back solely to preserve its own display
text across re-renders; no decision path ever consumes it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from foreman.github import STATUS_MARKER, GitHub
from foreman.graph import Unit
from foreman.util import utc_now_iso, warn


def table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def fmt(cells: list[str]) -> str:
        return "  ".join(
            cell.ljust(widths[index]) for index, cell in enumerate(cells)
        ).rstrip()

    lines = [fmt(headers), fmt(["-" * w for w in widths])]
    lines.extend(fmt(row) for row in rows)
    return "\n".join(lines)


@dataclass
class UnitStatus:
    """One unit's snapshot for the status comment + summary row."""

    unit: Unit
    state: str  # dispatched | pr-open | ready-for-review | failed | blocked | held | waiting | merged
    branch: str = ""
    pr_url: str = ""
    checks: str = ""
    blockers: list[str] = field(default_factory=list)
    human_tasks: list[str] = field(default_factory=list)
    blocked_question: str = ""
    detail: str = ""


STATE_ICONS = {
    "dispatched": "🚧",
    "pr-open": "🔃",
    "ready-for-review": "✅",
    "failed": "⚠️",
    "blocked": "❓",
    "held": "⏸",
    "waiting": "⏳",
    "merged": "🎉",
    "completed": "✔",
    "skipped": "⏭",
    "not-armed": "🔒",
    "refused": "⛔",
}


# ── event log (#82) ──────────────────────────────────────────────────
#
# The status comment's second section: time-stamped, immutable past facts
# (provenance, like the foreman:dispatched PR label), newest first. The log
# is parsed back out of the prior body only so a re-render preserves it —
# never to decide anything.

EVENT_LOG_MARKER = "<!-- foreman:event-log -->"
MAX_EVENTS = 50
# GitHub's comment cap is 65,536 chars; the snapshot section is bounded by
# blockers/human tasks, so capping the log alone leaves ample headroom.
MAX_LOG_CHARS = 50_000
# One entry can never eat the budget: an oversized failure detail must not
# make the whole write exceed GitHub's cap — a swallowed write failure would
# drop the very event (usually a failure) most worth recording.
MAX_EVENT_CHARS = 2_000
_EVENT_RE = re.compile(r"^- \S+ — .+$")
_EVENT_SEP = " — "


def parse_event_log(prior_body: str | None) -> list[str]:
    """Event lines (newest first) from a prior comment body. A missing
    marker or malformed content yields an empty log — never raises, because
    a display-only read must not be able to fail a run."""
    try:
        if not prior_body or EVENT_LOG_MARKER not in prior_body:
            return []
        tail = prior_body.split(EVENT_LOG_MARKER, 1)[1]
        return [line for line in tail.splitlines() if _EVENT_RE.match(line)]
    except Exception:  # pragma: no cover — defensive: parsing never fails a run
        return []


def _event_text(line: str) -> str:
    return line.split(_EVENT_SEP, 1)[1] if _EVENT_SEP in line else line


def _display(text: str) -> str:
    """Snapshot/event-safe rendering of semi-trusted text (exception strings,
    issue-derived details): strip the section markers a hostile string could
    use to forge a log boundary, and clamp runaway lengths so no single field
    can push the comment past GitHub's cap and silently lose the write."""
    text = text.replace(STATUS_MARKER, "").replace(EVENT_LOG_MARKER, "")
    if len(text) > MAX_EVENT_CHARS:
        text = text[: MAX_EVENT_CHARS - 1] + "…"
    return text


def append_event(events: list[str], text: str) -> list[str]:
    """Prepend a time-stamped event unless the newest one already says the
    same thing (timestamp ignored) — that dedup is what keeps per-tick
    shepherd appends spam-free. Oldest entries drop past the caps."""
    # One event, one line: details often carry newlines (stringified
    # exceptions), which would break the line grammar — and with it the
    # dedup that keeps repeated ticks from re-appending the same fact.
    text = _display(" ".join(text.split()))
    if events and _event_text(events[0]) == text:
        return events
    out = [f"- {utc_now_iso()}{_EVENT_SEP}{text}", *events][:MAX_EVENTS]
    while len(out) > 1 and len("\n".join(out)) > MAX_LOG_CHARS:
        out.pop()
    return out


def _render_log(events: list[str]) -> list[str]:
    return ["", EVENT_LOG_MARKER, "## Event log", "", *events]


def status_comment_body(status: UnitStatus, events: list[str] | None = None) -> str:
    icon = STATE_ICONS.get(status.state, "•")
    lines = [
        STATUS_MARKER,
        "",
        f"**Foreman unit status: {icon} {status.state}**",
        "",
    ]
    # Every interpolated field goes through _display: several are
    # semi-trusted (stringified exceptions, issue-derived questions/tasks)
    # and must not be able to forge a section boundary or blow the cap.
    if status.branch:
        lines.append(f"- Branch: `{_display(status.branch)}`")
    if status.pr_url:
        lines.append(f"- PR: {_display(status.pr_url)}")
    if status.checks:
        lines.append(f"- Checks: {_display(status.checks)}")
    for blocker in status.blockers:
        lines.append(f"- Blocker: {_display(blocker)}")
    if status.blocked_question:
        lines.append("")
        lines.append("**BLOCKED — needs a human answer:**")
        lines.append("")
        lines.append("> " + _display(status.blocked_question).replace("\n", "\n> "))
    if status.human_tasks:
        lines.append("")
        lines.append("**Human-only tasks (foreman never attempts these):**")
        lines.append("")
        for task in status.human_tasks:
            lines.append(f"- [ ] {_display(task)}")
    if status.detail:
        lines.append("")
        lines.append(_display(status.detail))
    lines.append("")
    lines.append(
        f"_Updated {utc_now_iso()} — this comment is edited in place by foreman; "
        "it is display-only and never read back for decisions._"
    )
    if events:
        lines.extend(_render_log(events))
    return "\n".join(lines) + "\n"


def update_status_comment(
    gh: GitHub, status: UnitStatus, event: str | None = None
) -> None:
    """Rewrite the snapshot, optionally recording one event. The prior log is
    carried forward unconditionally — an event-less refresh (the dispatch
    conclusion) must never wipe the initiation entry."""
    try:
        prior = gh.find_status_comment(status.unit.number)
        events = parse_event_log((prior or {}).get("body"))
        if event:
            events = append_event(events, event)
        # Thread the found id through: a second lookup inside the upsert
        # could flake after this read succeeded and silently drop the event.
        gh.upsert_status_comment(
            status.unit.number,
            status_comment_body(status, events),
            comment_id=(prior or {}).get("id"),
        )
    except Exception as exc:  # display-only: never fail a run over a comment
        warn(f"#{status.unit.number}: status comment update failed: {exc}")


def append_status_event(gh: GitHub, issue_number: int, event: str) -> None:
    """Record an event without touching the snapshot — for callers that hold
    no `Unit` to re-render one (shepherd). The snapshot section is preserved
    verbatim; a missing comment gets a log-only one."""
    try:
        prior = gh.find_status_comment(issue_number)
        body = (prior or {}).get("body") or ""
        events = parse_event_log(body)
        appended = append_event(events, event)
        if prior is not None and appended == events:
            # Deduped: identical content — don't burn a PATCH per tick.
            return
        snapshot = (
            body.split(EVENT_LOG_MARKER, 1)[0].rstrip("\n") if body else STATUS_MARKER
        )
        gh.upsert_status_comment(
            issue_number,
            "\n".join([snapshot, *_render_log(appended)]) + "\n",
            comment_id=(prior or {}).get("id"),
        )
    except Exception as exc:  # display-only: never fail a run over a comment
        warn(f"#{issue_number}: status event append failed: {exc}")


def summary_table(statuses: list[UnitStatus]) -> str:
    rows = []
    for status in statuses:
        icon = STATE_ICONS.get(status.state, "•")
        rows.append(
            [
                f"#{status.unit.number}",
                f"{icon} {status.state}",
                status.branch or "-",
                status.pr_url or "-",
                status.detail[:60] if status.detail else "-",
            ]
        )
    return table(["unit", "status", "branch", "pr", "detail"], rows)


def overall_snapshot(
    *,
    statuses: list[UnitStatus],
    merge_order: list[tuple[int, str]],
    human_tasks: dict[int, list[str]],
    blocked: dict[int, str],
    outcomes: list[tuple[int, str, str]],
) -> str:
    """Untargeted `foreman status` (#84): open foreman PRs + in-flight units,
    the consolidated human-action queue, and recent terminal outcomes — one
    snapshot with no --milestone/--issue."""
    lines: list[str] = ["Foreman overall status", ""]
    lines.append("Open foreman PRs and in-flight units:")
    lines.append(summary_table(statuses) if statuses else "  (none in flight)")
    lines.append("")
    lines.append(
        human_queue(
            merge_order=merge_order,
            human_tasks=human_tasks,
            blocked=blocked,
            environmental={},
        )
    )
    lines.append("")
    lines.append("Recent outcomes:")
    if outcomes:
        for number, state, detail in outcomes:
            # Outcome states arrive prefixed (agent:<status>) — icon by
            # the underlying status, honest prefixed label in the text.
            icon = STATE_ICONS.get(state.removeprefix("agent:"), "•")
            suffix = f": {detail}" if detail else ""
            lines.append(f"  {icon} #{number} {state}{suffix}")
    else:
        lines.append("  (none recorded)")
    return "\n".join(lines)


def human_queue(
    *,
    merge_order: list[tuple[int, str]],
    human_tasks: dict[int, list[str]],
    blocked: dict[int, str],
    environmental: dict[int, str],
) -> str:
    """The consolidated 'what needs a human' list (most-repeated chore in M4)."""
    lines: list[str] = ["Human action queue:"]
    if merge_order:
        lines.append("")
        lines.append("  Ready for human review (suggested dependency-aware order):")
        for position, (number, url) in enumerate(merge_order, 1):
            lines.append(f"    {position}. #{number}  {url}")
    if blocked:
        lines.append("")
        lines.append("  Blocked questions:")
        for number, question in sorted(blocked.items()):
            first = question.strip().splitlines()[0] if question.strip() else ""
            lines.append(f"    - #{number}: {first}")
    if human_tasks:
        lines.append("")
        lines.append("  Human-only tasks:")
        for number, tasks in sorted(human_tasks.items()):
            for task in tasks:
                lines.append(f"    - #{number}: {task}")
    if environmental:
        lines.append("")
        lines.append("  Environmental failures (fix the environment, not the code):")
        for number, detail in sorted(environmental.items()):
            lines.append(f"    - #{number}: {detail}")
    if len(lines) == 1:
        lines.append("  (empty — nothing waiting on a human)")
    return "\n".join(lines)
