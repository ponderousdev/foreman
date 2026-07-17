"""Dispatch through the Runner seam (#22): the per-unit lock, dispatch-meta
round-trip, and _conclude's classification of runner outcomes. Uses the mock
runner and a stub handoff — process mechanics belong to test_local_runner."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from foreman import dispatch as dispatch_mod
from foreman import runner as runner_mod
from foreman.backend import BackendResult
from foreman.config import Config
from foreman.graph import Unit
from foreman.handoff import WORKFLOW_HUMAN_ONLY
from foreman.runner import Selection
from tests.mock_runner import MockRunner


def make_unit(number: int = 5) -> Unit:
    return Unit(
        number=number,
        title="test unit",
        state="OPEN",
        state_reason=None,
        body="## Acceptance Criteria\n\n- works [CI]\n",
        url=f"https://example.invalid/{number}",
        labels=[],
        issue_type=None,
        milestone=None,
        parent=None,
    )


class StubHandoff:
    def __init__(
        self, *, ahead: int = 1, clean: bool = True, wf: list[str] | None = None
    ):
        self.ahead = ahead
        self.clean = clean
        self.wf = wf or []
        self.pushed: list[tuple[str, str, bool]] = []

    def collect(self) -> None:
        return None

    def is_clean(self) -> bool:
        return self.clean

    def commits_ahead(self, base_ref: str) -> int:
        return self.ahead

    def workflow_paths(self, base_ref: str) -> list[str]:
        return list(self.wf)

    def push(self, remote_name: str, branch: str, *, first: bool) -> None:
        self.pushed.append((remote_name, branch, first))


class UnitLockExclusion(unittest.TestCase):
    def test_second_acquire_fails_until_release(self):
        cfg = Config()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = runner_mod.UnitLock(cfg, root, 5)
            second = runner_mod.UnitLock(cfg, root, 5)
            other_unit = runner_mod.UnitLock(cfg, root, 6)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())  # double-dispatch impossible
            self.assertTrue(other_unit.acquire())  # per-unit, not global
            first.release()
            self.assertTrue(second.acquire())
            second.release()
            other_unit.release()


class DispatchMeta(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            dispatch_mod._write_dispatch_meta(
                run_dir, recorded_hash="abc", base_sha="def", branch="foreman/feat/5-x"
            )
            meta = dispatch_mod._read_dispatch_meta(run_dir)
            self.assertEqual(meta["spec_hash"], "abc")
            self.assertEqual(meta["base_sha"], "def")
            self.assertEqual(meta["branch"], "foreman/feat/5-x")

    def test_missing_meta_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(dispatch_mod._read_dispatch_meta(Path(tmp)), {})


class ConcludeClassifications(unittest.TestCase):
    """_conclude routes runner outcomes to honest unit classifications.
    gh=None is safe on these paths: they return before any GitHub call."""

    def run_conclude(
        self,
        result: BackendResult,
        *,
        handoff: StubHandoff | None = None,
        verify_cmd: list[str] | None = None,
    ):
        cfg = Config()
        if verify_cmd:
            cfg.verify = {"default": verify_cmd}
        ho = handoff or StubHandoff()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wt = root / "wt"
            wt.mkdir()
            run_dir = root / "run"
            run_dir.mkdir()
            selection = Selection(
                runner=MockRunner(),
                make_handoff=lambda workdir, handle: ho,
                refusal=lambda required: None,
            )
            return dispatch_mod._conclude(
                None,  # type: ignore[arg-type]
                cfg,
                root,
                selection,
                make_unit(),
                wt,
                run_dir,
                result,
                branch="foreman/feat/5-test-unit",
                base="origin/main",
                recorded_hash="hash",
                base_sha="sha",
                timeout_min=90,
                mode="labels",
            )

    def test_quota_wait_is_waiting_not_failed(self):
        outcome = self.run_conclude(BackendResult(returncode=1, quota_wait=True))
        self.assertEqual(outcome.status, "waiting")

    def test_timeout_reports_group_termination_only(self):
        outcome = self.run_conclude(BackendResult(returncode=143, timed_out=True))
        self.assertEqual(outcome.status, "failed")
        self.assertIn("process group terminated", outcome.detail)
        self.assertIn("descendants may survive", outcome.detail)

    def test_abnormal_is_never_reported_as_an_exit_code(self):
        outcome = self.run_conclude(BackendResult(returncode=None, abnormal=True))
        self.assertEqual(outcome.status, "failed")
        self.assertIn("abnormal", outcome.detail)
        self.assertIn("no recorded exit status", outcome.detail)

    def test_nonzero_exit_fails_with_the_code(self):
        outcome = self.run_conclude(BackendResult(returncode=7))
        self.assertEqual(outcome.status, "failed")
        self.assertIn("agent exited 7", outcome.detail)

    def test_missing_contract_fails(self):
        outcome = self.run_conclude(BackendResult(returncode=0))
        self.assertEqual(outcome.status, "failed")
        self.assertIn("result contract invalid", outcome.detail)

    def _completed_result_files(self, run_dir: Path) -> None:
        (run_dir / "result.json").write_text(
            '{"schema": 1, "status": "completed", "summary": "s", '
            '"handoff": "h", "ac_test_map": [{"criterion": "c", "tests": ["t"]}]}',
            "utf-8",
        )

    def test_workflow_touching_diff_fails_human_only_before_push(self):
        cfg = Config()
        ho = StubHandoff(wf=[".github/workflows/ci.yml"])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wt = root / "wt"
            wt.mkdir()
            run_dir = root / "run"
            run_dir.mkdir()
            self._completed_result_files(run_dir)
            selection = Selection(
                runner=MockRunner(),
                make_handoff=lambda workdir, handle: ho,
                refusal=lambda required: None,
            )
            outcome = dispatch_mod._conclude(
                None,  # type: ignore[arg-type]
                cfg,
                root,
                selection,
                make_unit(),
                wt,
                run_dir,
                BackendResult(returncode=0),
                branch="b",
                base="origin/main",
                recorded_hash="hash",
                base_sha="sha",
                timeout_min=90,
                mode="labels",
            )
        self.assertEqual(outcome.status, "failed")
        self.assertIn(WORKFLOW_HUMAN_ONLY.split(" — ")[0], outcome.detail)
        self.assertIn(".github/workflows/ci.yml", outcome.detail)
        self.assertEqual(ho.pushed, [])  # detected BEFORE any push

    def test_verify_failure_fails_before_any_push(self):
        cfg = Config()
        ho = StubHandoff()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wt = root / "wt"
            wt.mkdir()
            run_dir = root / "run"
            run_dir.mkdir()
            self._completed_result_files(run_dir)
            cfg.verify = {"default": ["false"]}
            selection = Selection(
                runner=MockRunner(),
                make_handoff=lambda workdir, handle: ho,
                refusal=lambda required: None,
            )
            outcome = dispatch_mod._conclude(
                None,  # type: ignore[arg-type]
                cfg,
                root,
                selection,
                make_unit(),
                wt,
                run_dir,
                BackendResult(returncode=0),
                branch="b",
                base="origin/main",
                recorded_hash="hash",
                base_sha="sha",
                timeout_min=90,
                mode="labels",
            )
        self.assertEqual(outcome.status, "failed")
        self.assertIn("verification failed", outcome.detail)
        self.assertEqual(ho.pushed, [])


if __name__ == "__main__":
    unittest.main()
