"""Local triage (#37): `foreman attach --unit N` under local resumes the
preserved worktree/session or says precisely how — never appears to succeed
while doing nothing."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from foreman import cli
from foreman.config import Config


class Args:
    unit = 5
    execute = False


def with_context(root: Path, cfg: Config):
    gh = object()
    return mock.patch.object(cli, "_context", return_value=(cfg, root, gh))


class LocalAttach(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.cfg = Config(runner="local")

    def _worktree(self):
        wt = self.root / self.cfg.worktrees_dir / "5-a-unit"
        wt.mkdir(parents=True)
        return wt

    def _run(self):
        buf = io.StringIO()
        with with_context(self.root, self.cfg), redirect_stdout(buf):
            rc = cli.cmd_attach(Args())
        return rc, buf.getvalue()

    def test_no_worktree_fails_not_silently_succeeds(self):
        rc, _out = self._run()
        self.assertEqual(rc, 1)

    def test_resumes_with_session_ref_when_present(self):
        self.cfg.billing = "api"
        wt = self._worktree()
        run_dir = self.root / self.cfg.runtime_dir / "units" / "5"
        run_dir.mkdir(parents=True)
        (run_dir / "session").write_text("SESSION_REF=sess-123\n", "utf-8")
        (run_dir / "run_started.json").write_text(
            json.dumps({"backend": "claude-code-deepseek"}), encoding="utf-8"
        )
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("foreman attach --unit 5 --execute", out)
        self.assertNotIn("sess-123", out)
        self.assertIn(str(wt), out)

    def test_execute_launches_recorded_adapter_with_allowlisted_environment(self):
        self.cfg.billing = "api"
        wt = self._worktree()
        run_dir = self.root / self.cfg.runtime_dir / "units" / "5"
        run_dir.mkdir(parents=True)
        session_file = run_dir / "session"
        session_file.write_text("SESSION_REF=sess-123\n", "utf-8")
        (run_dir / "run_started.json").write_text(
            json.dumps({"backend": "claude-code-deepseek"}), encoding="utf-8"
        )
        args = Args()
        args.execute = True
        completed = mock.Mock(returncode=7)
        with (
            with_context(self.root, self.cfg),
            mock.patch.object(
                cli.backend_mod, "agent_env", return_value={"GH_TOKEN": "read-only"}
            ) as agent_env,
            mock.patch.object(cli.backend_mod, "capabilities", return_value={"attach"}),
            mock.patch.object(cli.subprocess, "run", return_value=completed) as run,
        ):
            rc = cli.cmd_attach(args)
        self.assertEqual(rc, 7)
        agent_env.assert_called_once_with(self.cfg)
        call = run.call_args
        self.assertEqual(
            call.args[0][1:], ["attach", "--session-file", str(session_file)]
        )
        self.assertIn("claude-code-deepseek.sh", call.args[0][0])
        self.assertEqual(call.kwargs["cwd"], wt)
        self.assertEqual(
            call.kwargs["env"],
            {"GH_TOKEN": "read-only", "FOREMAN_BILLING": "api"},
        )
        self.assertFalse(call.kwargs["check"])

    def test_without_session_ref_gives_exact_manual_path(self):
        wt = self._worktree()
        run_dir = self.root / self.cfg.runtime_dir / "units" / "5"
        run_dir.mkdir(parents=True)
        (run_dir / "resume-state.md").write_text("# state\n", "utf-8")
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn(str(wt), out)
        self.assertIn("foreman attach --unit 5 --execute", out)
        self.assertIn("resume-state.md", out)

    def test_non_local_runner_is_explicitly_unimplemented(self):
        self.cfg = Config(runner="sprite")
        with mock.patch.object(cli, "error") as err:
            with with_context(self.root, self.cfg):
                rc = cli.cmd_attach(Args())
        self.assertEqual(rc, 1)
        self.assertTrue(err.called)


if __name__ == "__main__":
    unittest.main()
