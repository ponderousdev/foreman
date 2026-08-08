"""Result-contract validation: the agent→foreman handoff channel."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from foreman import backend as backend_mod

VALID = {
    "schema": 1,
    "status": "completed",
    "summary": "Did the thing.",
    "handoff": "New API at x().",
    "human_tasks": ["verify dashboard"],
    "proposed_pr_title": "feat(x): do the thing",
    "ac_test_map": [
        {"criterion": "parses config [CI]", "tests": ["tests/test_x.py::test_a"]}
    ],
    "blocked_question": None,
}


class ResultContract(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.run_dir = base / "run"
        self.worktree = base / "wt"
        self.run_dir.mkdir()
        self.worktree.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, data) -> None:
        (self.run_dir / "result.json").write_text(json.dumps(data), encoding="utf-8")

    def test_valid_completed(self):
        self.write(VALID)
        contract, errors = backend_mod.read_result(self.run_dir, self.worktree)
        self.assertEqual(errors, [])
        self.assertEqual(contract.status, "completed")
        self.assertEqual(contract.human_tasks, ["verify dashboard"])

    def test_missing_file_without_blocked_md_is_an_error(self):
        contract, errors = backend_mod.read_result(self.run_dir, self.worktree)
        self.assertIsNone(contract)
        self.assertTrue(errors)

    def test_blocked_md_fallback(self):
        (self.worktree / "BLOCKED.md").write_text("Which auth flow?", encoding="utf-8")
        contract, errors = backend_mod.read_result(self.run_dir, self.worktree)
        self.assertEqual(errors, [])
        self.assertEqual(contract.status, "blocked")
        self.assertEqual(contract.blocked_question, "Which auth flow?")

    def test_invalid_json(self):
        (self.run_dir / "result.json").write_text("{nope", encoding="utf-8")
        contract, errors = backend_mod.read_result(self.run_dir, self.worktree)
        self.assertIsNone(contract)
        self.assertTrue(any("valid JSON" in e for e in errors))

    def test_completed_requires_summary_and_ac_map(self):
        data = dict(VALID, summary="", ac_test_map=[])
        self.write(data)
        contract, errors = backend_mod.read_result(self.run_dir, self.worktree)
        self.assertIsNone(contract)
        self.assertTrue(any("summary" in e for e in errors))
        self.assertTrue(any("ac_test_map" in e for e in errors))

    def test_blocked_requires_question(self):
        self.write({"schema": 1, "status": "blocked"})
        contract, errors = backend_mod.read_result(self.run_dir, self.worktree)
        self.assertIsNone(contract)
        self.assertTrue(errors)

    def test_bad_schema_and_status(self):
        self.write({"schema": 2, "status": "done?"})
        contract, errors = backend_mod.read_result(self.run_dir, self.worktree)
        self.assertIsNone(contract)
        self.assertTrue(any("schema" in e for e in errors))


class SessionCost(unittest.TestCase):
    def test_stale_cost_not_rebilled_past_offset(self):
        # #54: the session file persists across resumes; a run that died
        # before emitting its own COST_USD must not be billed the previous
        # invocation's line. SESSION_REF still resolves from the whole file.
        from foreman.runner import ExitStatus

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "unit"
            run_dir.mkdir()
            content = "SESSION_REF=abc\nCOST_USD=5.00\n"
            backend_mod.session_ledger(run_dir).write_text(content, encoding="utf-8")
            stale = backend_mod.result_from_wait(
                run_dir,
                ExitStatus(code=124),
                timed_out=True,
                session_offset=len(content),
            )
            self.assertIsNone(stale.cost_usd)
            self.assertEqual(stale.session_ref, "abc")
            fresh = backend_mod.result_from_wait(
                run_dir, ExitStatus(code=0), timed_out=False
            )
            self.assertEqual(fresh.cost_usd, 5.0)


class Adjudication(unittest.TestCase):
    """The adjudication sidecar (#46): the agent records dispositions; the
    validated record is what authorizes foreman's own thread writes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, data) -> None:
        (self.run_dir / backend_mod.ADJUDICATION_FILE).write_text(
            json.dumps(data), encoding="utf-8"
        )

    def test_valid_dispositions(self):
        self.write(
            {
                "schema": 1,
                "dispositions": [
                    {
                        "thread_id": "t-1",
                        "disposition": "applied",
                        "note": "applied in abc1234",
                    },
                    {"thread_id": "t-2", "disposition": "declined", "note": "wrong"},
                ],
            }
        )
        dispositions, errors = backend_mod.read_adjudication(self.run_dir)
        self.assertEqual(errors, [])
        self.assertEqual(
            [(d.thread_id, d.disposition) for d in dispositions],
            [("t-1", "applied"), ("t-2", "declined")],
        )

    def test_missing_file_is_an_error(self):
        dispositions, errors = backend_mod.read_adjudication(self.run_dir)
        self.assertIsNone(dispositions)
        self.assertTrue(errors)

    def test_empty_list_is_an_error(self):
        self.write({"schema": 1, "dispositions": []})
        dispositions, errors = backend_mod.read_adjudication(self.run_dir)
        self.assertIsNone(dispositions)
        self.assertTrue(any("non-empty" in e for e in errors))

    def test_unknown_disposition_and_empty_note_fail(self):
        self.write(
            {
                "schema": 1,
                "dispositions": [
                    {"thread_id": "t-1", "disposition": "maybe", "note": "x"},
                    {"thread_id": "t-2", "disposition": "applied", "note": "  "},
                ],
            }
        )
        dispositions, errors = backend_mod.read_adjudication(self.run_dir)
        self.assertIsNone(dispositions)
        self.assertTrue(any("disposition must be one of" in e for e in errors))
        self.assertTrue(any("note must be a non-empty" in e for e in errors))

    def test_duplicate_thread_ids_fail(self):
        entry = {"thread_id": "t-1", "disposition": "declined", "note": "n"}
        self.write({"schema": 1, "dispositions": [entry, dict(entry)]})
        dispositions, errors = backend_mod.read_adjudication(self.run_dir)
        self.assertIsNone(dispositions)
        self.assertTrue(any("duplicate" in e for e in errors))

    def test_applied_parses_the_named_commit(self):
        self.write(
            {
                "schema": 1,
                "dispositions": [
                    {
                        "thread_id": "t-1",
                        "disposition": "applied",
                        "note": "applied in abc1234",
                    }
                ],
            }
        )
        dispositions, errors = backend_mod.read_adjudication(self.run_dir)
        self.assertEqual(errors, [])
        self.assertEqual(dispositions[0].applied_sha, "abc1234")

    def test_applied_without_a_commit_fails(self):
        # "done" would let a fixless resolution slip through — an applied
        # note must name its commit so the shepherd can prove it exists.
        self.write(
            {
                "schema": 1,
                "dispositions": [
                    {"thread_id": "t-1", "disposition": "applied", "note": "done"}
                ],
            }
        )
        dispositions, errors = backend_mod.read_adjudication(self.run_dir)
        self.assertIsNone(dispositions)
        self.assertTrue(any("must name the commit" in e for e in errors))
        # Declined dispositions carry reasoning, not a sha — unaffected.
        self.write(
            {
                "schema": 1,
                "dispositions": [
                    {"thread_id": "t-1", "disposition": "declined", "note": "done"}
                ],
            }
        )
        dispositions, errors = backend_mod.read_adjudication(self.run_dir)
        self.assertEqual(errors, [])
        self.assertIsNone(dispositions[0].applied_sha)


if __name__ == "__main__":
    unittest.main()
