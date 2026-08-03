"""`foreman status` (#84): with no --milestone/--issue it prints an overall
snapshot (open foreman PRs + in-flight units, the human-action queue, and
recent terminal outcomes) and exits 0; the targeted forms are unchanged."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from foreman import report
from foreman.cli import _parser, _status_overall
from foreman.config import Config
from foreman.graph import Unit
from tests.fakes import issue_json, make_github, pr_json


def bare_unit(number: int) -> Unit:
    return Unit(
        number=number,
        title=f"U{number}",
        state="OPEN",
        state_reason=None,
        body="",
        url="",
        labels=[],
        issue_type=None,
        milestone=None,
        parent=None,
    )


def pr_view(number: int, *, unit: int, labels: list[str] | None = None) -> dict:
    return {
        "number": number,
        "title": f"feat: unit {unit}",
        "url": f"https://github.com/owner/repo/pull/{number}",
        "state": "OPEN",
        "labels": [{"name": name} for name in labels or []],
        "headRefName": f"foreman/feat/{unit}-x",
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "mergeStateStatus": "CLEAN",
    }


def run_dir(root: Path, number: int, result: dict | None) -> None:
    unit_dir = root / ".foreman" / "units" / str(number)
    unit_dir.mkdir(parents=True, exist_ok=True)
    if result is not None:
        (unit_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")


class ParserAcceptsNoTarget(unittest.TestCase):
    def test_status_without_target_parses(self):
        args = _parser().parse_args(["status"])
        self.assertIsNone(args.milestone)
        self.assertIsNone(args.issue)

    def test_targeted_forms_still_parse(self):
        issue_args = _parser().parse_args(["status", "--issue", "5"])
        self.assertEqual(issue_args.issue, 5)
        ms_args = _parser().parse_args(["status", "--milestone", "M4"])
        self.assertEqual(ms_args.milestone, "M4")

    def test_dispatch_still_requires_a_target(self):
        # The relaxation is scoped to `status`; other subcommands are unchanged.
        with self.assertRaises(SystemExit):
            _parser().parse_args(["dispatch"])


class OverallSnapshot(unittest.TestCase):
    def _run(self, gh, root: Path) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = _status_overall(Config(), root, gh)
        return rc, buf.getvalue()

    def test_empty_world_prints_snapshot_and_exits_zero(self):
        cfg = Config()
        gh, runner = make_github(cfg)
        runner.when(["pr", "list"], [])
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = self._run(gh, Path(tmp))
        self.assertEqual(rc, 0)
        self.assertIn("Foreman overall status", out)
        self.assertIn("(none in flight)", out)
        self.assertIn("(none recorded)", out)

    def test_open_pr_becomes_an_in_flight_row(self):
        cfg = Config()
        gh, runner = make_github(cfg)
        runner.when(["pr", "list"], [pr_json(7, unit=3, merged=False)])
        runner.when(["issue", "view", "3"], issue_json(3, title="Unit three"))
        runner.when(["pr", "view", "7"], pr_view(7, unit=3))
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = self._run(gh, Path(tmp))
        self.assertEqual(rc, 0)
        self.assertIn("#3", out)
        self.assertIn("pr-open", out)
        self.assertIn("foreman/feat/3-x", out)

    def test_ready_to_merge_pr_enters_merge_queue(self):
        cfg = Config()
        gh, runner = make_github(cfg)
        runner.when(["pr", "list"], [pr_json(7, unit=3, merged=False)])
        runner.when(["issue", "view", "3"], issue_json(3))
        runner.when(["pr", "view", "7"], pr_view(7, unit=3, labels=["ready-to-merge"]))
        # Readiness revalidates every shepherd predicate: green checks,
        # CLEAN merge state, and no unresolved review threads.
        gh.review_threads = lambda number: []  # type: ignore[assignment]
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = self._run(gh, Path(tmp))
        self.assertEqual(rc, 0)
        self.assertIn("ready-to-merge", out)
        self.assertIn("Pending merges", out)

    def test_stale_ready_label_with_unresolved_thread_is_not_queued(self):
        cfg = Config()
        gh, runner = make_github(cfg)
        runner.when(["pr", "list"], [pr_json(7, unit=3, merged=False)])
        runner.when(["issue", "view", "3"], issue_json(3))
        runner.when(["pr", "view", "7"], pr_view(7, unit=3, labels=["ready-to-merge"]))
        gh.review_threads = lambda number: [  # type: ignore[assignment]
            {"id": "t", "isResolved": False}
        ]
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = self._run(gh, Path(tmp))
        self.assertEqual(rc, 0)
        self.assertNotIn("Pending merges", out)

    def test_local_blocked_and_completed_dirs_are_reported(self):
        cfg = Config()
        gh, runner = make_github(cfg)
        runner.when(["pr", "list"], [])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir(
                root,
                11,
                {"schema": 1, "status": "blocked", "blocked_question": "which db?"},
            )
            runner.when(["issue", "view", "11"], issue_json(11))
            runner.when(["issue", "view", "12"], issue_json(12))
            run_dir(
                root,
                12,
                {
                    "schema": 1,
                    "status": "completed",
                    "summary": "did the thing",
                    "handoff": "the new API",
                    "ac_test_map": [{"criterion": "c", "tests": ["t"]}],
                },
            )
            rc, out = self._run(gh, root)
        self.assertEqual(rc, 0)
        self.assertIn("which db?", out)  # blocked question in the human queue
        self.assertIn("#12", out)  # completed outcome
        self.assertIn("did the thing", out)

    def test_unit_with_open_pr_is_not_double_counted_from_local_dir(self):
        cfg = Config()
        gh, runner = make_github(cfg)
        runner.when(["pr", "list"], [pr_json(7, unit=3, merged=False)])
        runner.when(["issue", "view", "3"], issue_json(3))
        runner.when(["pr", "view", "7"], pr_view(7, unit=3))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir(root, 3, {"schema": 1, "status": "completed", "summary": "stale"})
            rc, out = self._run(gh, root)
        self.assertEqual(rc, 0)
        # The open PR is the live state; the stale local result must not appear
        # as a recent outcome.
        self.assertIn("(none recorded)", out)
        self.assertNotIn("stale", out)


class OverallSnapshotRendering(unittest.TestCase):
    def test_sections_present_when_empty(self):
        out = report.overall_snapshot(
            statuses=[], merge_order=[], human_tasks={}, blocked={}, outcomes=[]
        )
        self.assertIn("Open foreman PRs and in-flight units:", out)
        self.assertIn("Human action queue:", out)
        self.assertIn("Recent outcomes:", out)

    def test_outcome_line_renders_number_state_and_detail(self):
        out = report.overall_snapshot(
            statuses=[report.UnitStatus(unit=bare_unit(3), state="pr-open")],
            merge_order=[],
            human_tasks={},
            blocked={},
            outcomes=[(9, "completed", "shipped it")],
        )
        self.assertIn("#9 completed: shipped it", out)


if __name__ == "__main__":
    unittest.main()
