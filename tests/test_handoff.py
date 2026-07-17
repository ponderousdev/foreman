"""SharedWorktreeHandoff against a real git repo — including the
workflow-diff tripwire (#21): detected before push, never a bare 403."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from foreman.handoff import SharedWorktreeHandoff


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(cwd), "LEFTHOOK": "0"},
    )
    return proc.stdout


class HandoffOnRealGit(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("hello\n", "utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "init")
        git(self.repo, "checkout", "-q", "-b", "unit-branch")
        self.ho = SharedWorktreeHandoff(self.repo)

    def test_clean_and_ahead_counting(self):
        self.assertTrue(self.ho.is_clean())
        self.assertEqual(self.ho.commits_ahead("main"), 0)
        (self.repo / "code.py").write_text("x = 1\n", "utf-8")
        self.assertFalse(self.ho.is_clean())
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "feat: code")
        self.assertTrue(self.ho.is_clean())
        self.assertEqual(self.ho.commits_ahead("main"), 1)

    def test_workflow_diff_is_detected_before_push(self):
        wf = self.repo / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text("name: probe\n", "utf-8")
        (self.repo / "code.py").write_text("x = 1\n", "utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "feat: touches workflows")
        touched = self.ho.workflow_paths("main")
        self.assertEqual(touched, [".github/workflows/ci.yml"])

    def test_non_workflow_diff_is_not_flagged(self):
        (self.repo / "code.py").write_text("x = 1\n", "utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "feat: plain code")
        self.assertEqual(self.ho.workflow_paths("main"), [])

    def test_collect_is_a_no_op_for_the_shared_worktree(self):
        self.assertIsNone(self.ho.collect())


if __name__ == "__main__":
    unittest.main()
