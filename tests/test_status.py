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
from foreman.cli import _parser, _scan_local_run, _status_overall, _status_targeted
from foreman.config import Config
from foreman.github import LEGACY_READY_FOR_REVIEW_LABEL, READY_FOR_REVIEW_LABEL
from foreman.graph import Target, Unit
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
    label_names = labels or []
    return {
        "number": number,
        "title": f"feat: unit {unit}",
        "url": f"https://github.com/owner/repo/pull/{number}",
        "state": "OPEN",
        "labels": [{"name": name} for name in label_names],
        "body": (
            "<!-- foreman:ready-head:head -->"
            if READY_FOR_REVIEW_LABEL in label_names
            else ""
        ),
        "headRefName": f"foreman/feat/{unit}-x",
        "headRefOid": "head",
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "mergeStateStatus": "CLEAN",
    }


def run_dir(
    root: Path, number: int, result: dict | None, *, concluded: bool = True
) -> None:
    unit_dir = root / ".foreman" / "units" / str(number)
    unit_dir.mkdir(parents=True, exist_ok=True)
    if result is not None:
        (unit_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    if concluded:
        # The wrapper's recorded exit — without it a contract-bearing run
        # is still live and renders as in-flight (89614 semantics).
        (unit_dir / "exit-status").write_text("0", encoding="utf-8")


def run_started(
    root: Path,
    number: int,
    *,
    started_at: str = "2026-08-03T00:00:00Z",
    died: bool = False,
) -> None:
    """An in-flight run: run_started.json but no contract yet. `died=True`
    drops the wrapper's exit-status sentinel (dead without a contract)."""
    unit_dir = root / ".foreman" / "units" / str(number)
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "run_started.json").write_text(
        json.dumps({"started_at": started_at}), encoding="utf-8"
    )
    if died:
        (unit_dir / "exit-status").write_text("1\n", encoding="utf-8")


def target_of(*numbers: int) -> Target:
    return Target(
        label="issue #" + "/".join(str(n) for n in numbers),
        units={n: bare_unit(n) for n in numbers},
        external_deps=set(),
    )


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

    def test_ready_for_review_pr_enters_human_review_queue(self):
        cfg = Config()
        gh, runner = make_github(cfg)
        runner.when(["pr", "list"], [pr_json(7, unit=3, merged=False)])
        runner.when(["issue", "view", "3"], issue_json(3))
        runner.when(
            ["pr", "view", "7"],
            pr_view(7, unit=3, labels=[READY_FOR_REVIEW_LABEL]),
        )
        # Readiness revalidates every shepherd predicate: green checks,
        # CLEAN merge state, and no unresolved review threads.
        gh.review_threads = lambda number: []  # type: ignore[assignment]
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = self._run(gh, Path(tmp))
        self.assertEqual(rc, 0)
        self.assertIn("ready-for-review", out)
        self.assertIn("Ready for human review", out)

    def test_legacy_ready_label_remains_visible_during_transition(self):
        cfg = Config()
        gh, runner = make_github(cfg)
        runner.when(["pr", "list"], [pr_json(7, unit=3, merged=False)])
        runner.when(["issue", "view", "3"], issue_json(3))
        runner.when(
            ["pr", "view", "7"],
            pr_view(7, unit=3, labels=[LEGACY_READY_FOR_REVIEW_LABEL]),
        )
        gh.review_threads = lambda number: []  # type: ignore[assignment]
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = self._run(gh, Path(tmp))
        self.assertEqual(rc, 0)
        self.assertIn("ready-for-review", out)
        self.assertIn("Ready for human review", out)

    def test_stale_ready_label_with_unresolved_thread_is_not_queued(self):
        cfg = Config()
        gh, runner = make_github(cfg)
        runner.when(["pr", "list"], [pr_json(7, unit=3, merged=False)])
        runner.when(["issue", "view", "3"], issue_json(3))
        runner.when(
            ["pr", "view", "7"],
            pr_view(7, unit=3, labels=[READY_FOR_REVIEW_LABEL]),
        )
        gh.review_threads = lambda number: [  # type: ignore[assignment]
            {"id": "t", "isResolved": False}
        ]
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = self._run(gh, Path(tmp))
        self.assertEqual(rc, 0)
        self.assertNotIn("Ready for human review", out)

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


class TargetedMatchesOverall(unittest.TestCase):
    """#94: the targeted and untargeted views must report the same state for
    a mid-run unit — both now fold in the same local-run evidence."""

    def _overall(self, gh, root: Path) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(_status_overall(Config(), root, gh), 0)
        return buf.getvalue()

    def _targeted(self, gh, root: Path, *numbers: int) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(
                _status_targeted(Config(), root, gh, target_of(*numbers)), 0
            )
        return buf.getvalue()

    def test_active_dispatch_reports_in_flight_in_both_views(self):
        # A run that has started but pushed nothing: the old targeted path
        # read this as a bare "waiting" while the overall view already called
        # it in-flight. Both must now agree.
        cfg = Config()
        gh, runner = make_github(cfg)
        runner.when(["pr", "list"], [])
        runner.when(["issue", "view", "90"], issue_json(90, title="Unit ninety"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_started(root, 90)
            overall = self._overall(gh, root)
            targeted = self._targeted(gh, root, 90)
        self.assertIn("in-flight (no contract)", overall)
        self.assertIn("in-flight (no contract)", targeted)
        # The old bug rendered the unit's state row as "⏳ waiting".
        self.assertNotIn("⏳ waiting", targeted)

    def test_died_without_contract_reports_agent_died_in_both_views(self):
        cfg = Config()
        gh, runner = make_github(cfg)
        runner.when(["pr", "list"], [])
        runner.when(["issue", "view", "90"], issue_json(90))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_started(root, 90, died=True)
            overall = self._overall(gh, root)
            targeted = self._targeted(gh, root, 90)
        self.assertIn("agent:died", overall)
        self.assertIn("agent:died", targeted)

    def test_no_local_run_still_reports_waiting_when_targeted(self):
        # The scheduling fallback survives: a unit with no run dir at all is
        # still "waiting" (nothing has been dispatched yet).
        cfg = Config()
        gh, runner = make_github(cfg)
        runner.when(["pr", "list"], [])
        with tempfile.TemporaryDirectory() as tmp:
            targeted = self._targeted(gh, Path(tmp), 90)
        self.assertIn("⏳ waiting", targeted)

    def test_targeted_status_revalidates_stale_ready_label(self):
        cfg = Config()
        gh, runner = make_github(cfg)
        runner.when(["pr", "list"], [pr_json(7, unit=3, merged=False)])
        stale = pr_view(7, unit=3, labels=[READY_FOR_REVIEW_LABEL])
        stale["statusCheckRollup"] = [{"status": "COMPLETED", "conclusion": "FAILURE"}]
        runner.when(["pr", "view", "7"], stale)
        with tempfile.TemporaryDirectory() as tmp:
            targeted = self._targeted(gh, Path(tmp), 3)
        self.assertIn("pr-open", targeted)
        self.assertNotIn("ready-for-review", targeted)
        self.assertNotIn("Ready for human review", targeted)


class LocalScanSingleSourced(unittest.TestCase):
    """#94 AC: the local-scan logic is single-sourced — one helper, both call
    sites. Assert both status paths route through _scan_local_run so they can
    never drift apart again."""

    def test_both_status_paths_call_the_shared_helper(self):
        import inspect

        for fn in (_status_overall, _status_targeted):
            self.assertIn("_scan_local_run", inspect.getsource(fn))

    def test_helper_classifies_active_and_dead_runs(self):
        cfg = Config()
        gh, runner = make_github(cfg)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_started(root, 1)
            active = _scan_local_run(cfg, root, gh, 1, has_open_pr=False)
            run_started(root, 2, died=True)
            dead = _scan_local_run(cfg, root, gh, 2, has_open_pr=False)
            missing = _scan_local_run(cfg, root, gh, 3, has_open_pr=False)
        self.assertEqual(active.state, "in-flight (no contract)")
        self.assertFalse(active.terminal)
        self.assertEqual(dead.state, "agent:died (no contract)")
        self.assertTrue(dead.terminal)
        self.assertIsNone(missing)

    def test_open_pr_suppresses_snapshot_but_keeps_queue_contribution(self):
        cfg = Config()
        gh, runner = make_github(cfg)
        runner.when(["issue", "view", "5"], issue_json(5))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir(
                root,
                5,
                {
                    "schema": 1,
                    "status": "completed",
                    "summary": "did it",
                    "handoff": "api",
                    "human_tasks": ["deploy the thing"],
                    "ac_test_map": [{"criterion": "c", "tests": ["t"]}],
                },
            )
            run = _scan_local_run(cfg, root, gh, 5, has_open_pr=True)
        # Represented by its PR row elsewhere: no snapshot state here...
        self.assertIsNone(run.state)
        # ...but the run's human work still reaches the queue.
        self.assertEqual(run.human_tasks, ["deploy the thing"])


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
