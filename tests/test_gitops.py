"""UnitGit routes every boundary git op through Runner.exec (#20) — no call
assumes `git -C <local path>`. A recording runner proves the contract; the
end-to-end behavior against a real repo is covered by test_handoff."""

from __future__ import annotations

import unittest
from pathlib import Path

from foreman.gitops import UnitGit
from foreman.runner import ExecResult


class RecordingRunner:
    name = "recording"

    def __init__(self, stdout: str = "", returncode: int = 0):
        self.calls: list[tuple[str, list[str]]] = []
        self._stdout = stdout
        self._rc = returncode

    def exec(self, handle, cmd: list[str]) -> ExecResult:
        # The workdir the op is scoped to travels on the handle, never as a
        # `git -C` argument in cmd.
        self.calls.append((str(handle.payload.get("workdir")), list(cmd)))
        return ExecResult(returncode=self._rc, stdout=self._stdout, stderr="")


class RoutesThroughRunner(unittest.TestCase):
    def test_ops_never_pass_git_dash_C_and_carry_workdir_on_the_handle(self):
        runner = RecordingRunner(stdout="")
        git = UnitGit(runner, Path("/wt/unit-5"))
        git.is_clean()
        git.commits_ahead("origin/main")
        git.workflow_paths("origin/main")
        for workdir, cmd in runner.calls:
            self.assertEqual(cmd[0], "git")
            self.assertNotIn("-C", cmd)  # never `git -C <path>`
            self.assertEqual(workdir, "/wt/unit-5")  # scoped via the handle

    def test_is_clean_reads_porcelain(self):
        self.assertTrue(UnitGit(RecordingRunner(stdout="  \n"), Path("/wt")).is_clean())
        self.assertFalse(
            UnitGit(RecordingRunner(stdout=" M a.py\n"), Path("/wt")).is_clean()
        )

    def test_commits_ahead_parses_count(self):
        self.assertEqual(
            UnitGit(RecordingRunner(stdout="3\n"), Path("/wt")).commits_ahead("b"), 3
        )
        self.assertEqual(
            UnitGit(RecordingRunner(stdout=""), Path("/wt")).commits_ahead("b"), 0
        )

    def test_workflow_paths_filters_blanks(self):
        runner = RecordingRunner(stdout=".github/workflows/ci.yml\n\n")
        self.assertEqual(
            UnitGit(runner, Path("/wt")).workflow_paths("b"),
            [".github/workflows/ci.yml"],
        )

    def test_push_raises_on_failure(self):
        from foreman.util import ForemanError

        git = UnitGit(RecordingRunner(returncode=1), Path("/wt"))
        with self.assertRaises(ForemanError):
            git.push("origin", "b", first=True)

    def test_clean_rebase_returns_true(self):
        self.assertTrue(
            UnitGit(RecordingRunner(returncode=0), Path("/wt")).rebase_onto("b")
        )


if __name__ == "__main__":
    unittest.main()
