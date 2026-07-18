"""Commit handoff: how agent commits reach Foreman's push (#21).

Local (v2.0): Foreman and the agent share the worktree, so reads are direct
and Foreman pushes with the write token — correctness and auditability here,
containment only under sprite (D3). The interface exists so the Sprite's
bundle strategy (v2.1: read-only clone in the guest, `git bundle` out,
applied to Foreman's clone, pushed by Foreman) slots in behind
`foreman.runner.create()` without touching a single dispatch call site.

Workflow diffs cannot be pushed — detect, don't discover: the write token
has no workflow permission, so GitHub rejects any push whose diff touches
`.github/workflows/`. `workflow_paths()` lets dispatch fail the unit early
with a clear human-only classification instead of a bare 403 at the last
step. Known consequence (spec, trust model): the shepherd cannot dispatch a
fix for workflow-caused red CI — workflow changes are permanently human-only
under this trust model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from foreman.gitops import UnitGit
from foreman.runner import Runner


@runtime_checkable
class CommitHandoff(Protocol):
    """Per-unit commit transport. Constructed by the selection layer
    (foreman.runner.create) so dispatch never knows which strategy runs."""

    def collect(self) -> None:
        """Bring the unit's commits into Foreman's reach (local: no-op —
        the worktree is shared; sprite: fetch + apply the bundle)."""
        ...

    def is_clean(self) -> bool: ...

    def commits_ahead(self, base_ref: str) -> int: ...

    def workflow_paths(self, base_ref: str) -> list[str]:
        """Paths under .github/workflows/ touched by the unit's diff."""
        ...

    def push(self, remote_name: str, branch: str, *, first: bool) -> None:
        """Foreman-owned push with the write token. Agents never push."""
        ...


class SharedWorktreeHandoff:
    """v2.0 local strategy: the agent committed straight into a worktree of
    Foreman's own clone. The git operations route through the runner (#20,
    via UnitGit) so no call assumes `git -C <local path>`."""

    def __init__(self, runner: Runner, workdir: Path):
        self.workdir = workdir
        self._git = UnitGit(runner, workdir)

    def collect(self) -> None:
        return None  # shared worktree: the commits are already here

    def is_clean(self) -> bool:
        return self._git.is_clean()

    def commits_ahead(self, base_ref: str) -> int:
        return self._git.commits_ahead(base_ref)

    def workflow_paths(self, base_ref: str) -> list[str]:
        return self._git.workflow_paths(base_ref)

    def push(self, remote_name: str, branch: str, *, first: bool) -> None:
        self._git.push(remote_name, branch, first=first)


WORKFLOW_HUMAN_ONLY = (
    "diff touches .github/workflows/ — workflow changes are human-only under "
    "this trust model (the write token has no workflow permission; the push "
    "would be rejected). Unit preserved; a human must land the workflow part."
)
