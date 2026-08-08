"""Vet liveness narration (#125): `foreman vet` acknowledges the analysis run
and narrates a terminal outcome through the same display-only progress seam as
dispatch — labeled `vet`, never `#0`, line-oriented on non-TTY."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from foreman import cli
from foreman.backend import BackendResult
from foreman.config import Config
from foreman.runner import Selection


class Args:
    post = False
    milestone = "7"
    issue = None


def _target():
    unit = SimpleNamespace(
        title="a unit",
        body="do the thing",
        sub_issues=[],
        required_capabilities=set(),
    )
    return SimpleNamespace(units={5: unit}, milestone_number=7, label="milestone-7")


class VetNarration(unittest.TestCase):
    def _run(self, result: BackendResult) -> tuple[int, str, mock.MagicMock]:
        cfg = Config(runner="local")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selection = Selection(
                runner=object(),  # type: ignore[arg-type]
                make_handoff=lambda _workdir, _handle: None,
                refusal=lambda _required: None,
            )
            buf = io.StringIO()
            with (
                mock.patch.object(cli, "_context", return_value=(cfg, root, object())),
                mock.patch.object(cli, "prepare_target", return_value=_target()),
                mock.patch.object(cli, "_concurrent_activity", return_value=[]),
                mock.patch.object(cli.runner_mod, "select", return_value=selection),
                mock.patch.object(cli.spec, "trusted_comments", return_value=([], 0)),
                mock.patch.object(cli.spec, "load_prompt", return_value="analyze"),
                mock.patch.object(cli.backend_mod, "assert_backend_version"),
                mock.patch.object(
                    cli.backend_mod, "adapter_path", return_value=Path("/x/claude")
                ),
                mock.patch.object(
                    cli.backend_mod, "run_backend", return_value=result
                ) as run_backend,
                redirect_stdout(buf),
            ):
                rc = cli.cmd_vet(Args())
        return rc, buf.getvalue(), run_backend

    def test_acknowledges_and_narrates_terminal_on_success(self):
        rc, out, run_backend = self._run(BackendResult(returncode=0))
        self.assertEqual(rc, 0)
        # Liveness rides run_backend's reporter seam — the same wiring
        # wait_with_heartbeat drives for dispatch (#83).
        self.assertIsNotNone(run_backend.call_args.kwargs["reporter"])
        vet_lines = [ln for ln in out.splitlines() if ln.startswith("foreman: vet: ")]
        self.assertEqual(len(vet_lines), 2)
        self.assertIn("analyzing milestone #7", vet_lines[0])
        self.assertIn("agent.log", vet_lines[0])
        self.assertIn("analysis complete", vet_lines[1])
        self.assertNotIn("\r", out)  # captured stdout is not a TTY → plain lines
        self.assertNotIn("#0", out)  # the reservation number never leaks

    def test_terminal_is_honest_on_failure_and_timeout(self):
        rc, out, _ = self._run(BackendResult(returncode=2))
        self.assertEqual(rc, 1)
        self.assertIn("foreman: vet: analysis failed — see agent log", out)
        rc, out, _ = self._run(BackendResult(returncode=None, timed_out=True))
        self.assertEqual(rc, 1)
        self.assertIn("foreman: vet: timed out", out)


if __name__ == "__main__":
    unittest.main()
