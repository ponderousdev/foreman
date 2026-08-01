"""Git at the unit boundary, routed through `Runner.exec` (#20).

Every git operation on a unit's worktree goes through the runner rather than a
hardcoded `git -C <local path>`, so a remote runner slots in at the same call
sites without touching them: under `local` the runner execs a subprocess in
the workdir; under `docker` it execs against the bind mount; under `sprite`
(v2.1) the same calls travel over the Fly API. Nothing here assumes the
worktree is a local path.

Commit *return* is the other, separate mechanism (the explicit CommitHandoff,
#21); this covers the read/rebase/push operations the supervisor performs at
the boundary.
"""

from __future__ import annotations

from pathlib import Path

from foreman.runner import ExecResult, Handle, Runner
from foreman.util import ForemanError


class UnitGit:
    """Git commands scoped to one unit's worktree, executed via the runner.

    A synthetic, workdir-only Handle is used so the existing `Runner.exec`
    (which runs in `payload["workdir"]`) serves both the dispatch handle case
    and the shepherd case, where the original agent process is long gone but
    the worktree remains."""

    def __init__(self, runner: Runner, workdir: Path):
        self._runner = runner
        self.workdir = workdir
        self._handle = Handle(
            runner=getattr(runner, "name", "runner"),
            unit=0,
            run_dir=str(workdir),
            payload={"workdir": str(workdir)},
        )

    def _git(self, *args: str) -> ExecResult:
        return self._runner.exec(self._handle, ["git", *args])

    def _checked_git(self, *args: str) -> ExecResult:
        result = self._git(*args)
        if result.returncode != 0:
            detail = result.stderr or result.stdout
            raise ForemanError(f"git {' '.join(args)} failed: {detail}")
        return result

    def is_clean(self) -> bool:
        return not self._checked_git("status", "--porcelain").stdout.strip()

    def commits_ahead(self, base_ref: str) -> int:
        out = self._checked_git(
            "rev-list", "--count", f"{base_ref}..HEAD"
        ).stdout.strip()
        return int(out or "0")

    def workflow_paths(self, base_ref: str) -> list[str]:
        """Paths under .github/workflows/ touched by the unit's diff (#21)."""
        out = self._checked_git(
            "diff",
            "--name-only",
            f"{base_ref}...HEAD",
            "--",
            ".github/workflows/",
        ).stdout
        return [line.strip() for line in out.splitlines() if line.strip()]

    def push(self, remote_name: str, branch: str, *, first: bool) -> None:
        args = ["push"]
        if first:
            args += ["-u", remote_name, branch]
        else:
            args += ["--force-with-lease", remote_name, branch]
        result = self._git(*args)
        if result.returncode != 0:
            raise ForemanError(
                f"git push failed for {branch}: {result.stderr or result.stdout}"
            )

    def commit_on_branch(self, sha: str) -> bool:
        """True when `sha` resolves to a commit that is an ancestor of the
        worktree's HEAD — the proof behind an `applied in <sha>` claim."""
        if self._git("cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
            return False
        return self._git("merge-base", "--is-ancestor", sha, "HEAD").returncode == 0

    def merge_tree_conflicts(self, base_ref: str) -> list[str]:
        """Deterministic conflict enumeration (dry run; the tree is untouched)."""
        head = self._checked_git("rev-parse", "HEAD").stdout.strip()
        result = self._git("merge-tree", "--write-tree", "--name-only", base_ref, head)
        if result.returncode == 0:
            return []
        lines = [line for line in result.stdout.splitlines()[1:] if line.strip()]
        return lines or ["<unknown conflict>"]

    def rebase_onto(self, base_ref: str) -> bool:
        if self._git("rebase", base_ref).returncode != 0:
            self._git("rebase", "--abort")
            return False
        return True

    def empty_commit(self, message: str) -> None:
        result = self._git("commit", "--allow-empty", "-m", message)
        if result.returncode != 0:
            raise ForemanError(f"empty commit failed: {result.stderr or result.stdout}")

    def count_retrigger_commits(self, base_ref: str, subject: str) -> int:
        out = self._checked_git("log", f"{base_ref}..HEAD", "--format=%s").stdout
        return sum(1 for line in out.splitlines() if line.strip() == subject)
