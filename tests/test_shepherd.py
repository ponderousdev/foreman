"""Shepherd classification: check-rollup bucketing and the signature catalog."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from foreman import backend as backend_mod
from foreman import signatures as signatures_mod
from foreman.backend import BackendResult
from foreman.config import Config
from foreman.github import (
    DISPATCHED_LABEL,
    LEGACY_DISPATCHED_LABEL,
    LEGACY_READY_FOR_REVIEW_LABEL,
    READY_FOR_REVIEW_LABEL,
)
from foreman.runner import Selection
from foreman.shepherd import (
    PrWork,
    _demote_promoted_if_invalid,
    _promote_ready_head,
    _recover_interrupted_promotion,
    _resume_agent,
    _return_to_draft,
    automation_ready_now,
    classify_checks,
    open_foreman_prs,
    ready_for_review_now,
    shepherd_pr,
)
from foreman.util import ForemanError
from tests.fakes import make_github, pr_json
from tests.fakes import make_github as _mk


class ResumeAdapterSelection(unittest.TestCase):
    def test_shepherd_reuses_adapter_recorded_by_dispatch(self):
        cfg = Config(backend="claude", remote="origin")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = backend_mod.unit_dir(cfg, root, 5)
            (run_dir / "run_started.json").write_text(
                json.dumps({"backend": "claude-code-deepseek"}), encoding="utf-8"
            )
            backend_mod._record_backend_selection(cfg, root, 5, "claude-code-deepseek")
            backend_mod.session_ledger(run_dir).write_text(
                "SESSION_REF=deepseek-session\n", encoding="utf-8"
            )
            wt = root / "worktree"
            wt.mkdir()
            work = PrWork(
                10, 5, "foreman/feat/5-test", "https://example.invalid/10", "test"
            )
            selection = Selection(
                runner=object(),  # type: ignore[arg-type]
                make_handoff=lambda _workdir, _handle: None,
                refusal=lambda _required: None,
            )
            with (
                patch("foreman.shepherd.spec.load_prompt", return_value="fix it"),
                patch("foreman.shepherd._ensure_worktree", return_value=wt),
                patch(
                    "foreman.shepherd.backend_mod.capabilities", return_value={"resume"}
                ),
                patch("foreman.shepherd.backend_mod.assert_backend_version") as version,
                patch(
                    "foreman.shepherd.backend_mod.run_backend",
                    return_value=BackendResult(returncode=0),
                ) as run_backend,
            ):
                _resume_agent(
                    object(),  # type: ignore[arg-type]
                    cfg,
                    root,
                    selection,
                    work,
                    "review-fix",
                    {},
                )
            version.assert_called_once_with(cfg, "claude-code-deepseek")
            self.assertEqual(
                run_backend.call_args.args[3],
                backend_mod.adapter_path("claude-code-deepseek"),
            )
            self.assertEqual(
                run_backend.call_args.kwargs["resume_ref"], "deepseek-session"
            )


class ResumeAgentNarration(unittest.TestCase):
    """#125: every shepherd agent invocation narrates start + terminal through
    the _resume_agent funnel, labeled per unit/PR, line-oriented on non-TTY."""

    def _run(
        self, prompt_name: str, result: BackendResult
    ) -> tuple[str, list[str], object]:
        cfg = Config(backend="claude", remote="origin")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wt = root / "worktree"
            wt.mkdir()
            work = PrWork(
                10, 5, "foreman/feat/5-test", "https://example.invalid/10", "test"
            )
            selection = Selection(
                runner=object(),  # type: ignore[arg-type]
                make_handoff=lambda _workdir, _handle: None,
                refusal=lambda _required: None,
            )
            buf = io.StringIO()
            with (
                patch("foreman.shepherd.spec.load_prompt", return_value="fix it"),
                patch("foreman.shepherd._ensure_worktree", return_value=wt),
                patch("foreman.shepherd.backend_mod.capabilities", return_value=set()),
                patch("foreman.shepherd.backend_mod.assert_backend_version"),
                patch(
                    "foreman.shepherd.backend_mod.run_backend", return_value=result
                ) as run_backend,
                redirect_stdout(buf),
            ):
                _resume_agent(
                    object(),  # type: ignore[arg-type]
                    cfg,
                    root,
                    selection,
                    work,
                    prompt_name,
                    {},
                )
            reporter = run_backend.call_args.kwargs["reporter"]
        return (
            buf.getvalue(),
            [ln for ln in buf.getvalue().splitlines() if ln],
            reporter,
        )

    def test_narrates_start_and_terminal_with_pr_label(self):
        out, lines, reporter = self._run("shepherd-ci-fix", BackendResult(returncode=0))
        self.assertIsNotNone(reporter)  # heartbeat wiring rides run_backend
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(ln.startswith("foreman: #5 PR #10: ") for ln in lines))
        self.assertIn("repairing red CI", lines[0])
        self.assertIn("agent.log", lines[0])
        self.assertIn("agent finished", lines[1])
        self.assertNotIn("\r", out)  # captured stdout is not a TTY → plain lines

    def test_terminal_is_honest_about_timeout_and_failure(self):
        _out, lines, _ = self._run(
            "shepherd-rebase", BackendResult(returncode=None, timed_out=True)
        )
        self.assertIn("resolving rebase conflicts", lines[0])
        self.assertIn("agent timed out", lines[1])
        _out, lines, _ = self._run("shepherd-adjudicate", BackendResult(returncode=3))
        self.assertIn("adjudicating review threads", lines[0])
        self.assertIn("agent failed — exit=3", lines[1])


class LabelTransition(unittest.TestCase):
    def test_current_and_legacy_provenance_labels_are_discoverable_and_deduped(self):
        gh, _runner = make_github()
        current = pr_json(10, unit=1, merged=False)
        legacy = pr_json(11, unit=2, merged=False)
        calls = []

        def prs(*, label=None, head=None, state="open"):
            calls.append((label, state))
            if label == DISPATCHED_LABEL:
                return [current]
            if label == LEGACY_DISPATCHED_LABEL:
                return [legacy, current]
            return []

        gh.prs = prs  # type: ignore[method-assign]
        discovered = open_foreman_prs(gh)
        self.assertEqual([pr["number"] for pr in discovered], [10, 11])
        self.assertEqual([pr["_unit"] for pr in discovered], [1, 2])
        self.assertEqual(
            calls,
            [(DISPATCHED_LABEL, "open"), (LEGACY_DISPATCHED_LABEL, "open")],
        )

    def test_current_and_legacy_readiness_labels_are_live_revalidated(self):
        gh, _runner = make_github()
        gh.review_threads = lambda number: []  # type: ignore[assignment]
        base = {
            "number": 9,
            "state": "OPEN",
            "isDraft": False,
            "body": "<!-- foreman:ready-head:head -->",
            "headRefOid": "head",
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
            "mergeStateStatus": "CLEAN",
        }
        for label in (READY_FOR_REVIEW_LABEL, LEGACY_READY_FOR_REVIEW_LABEL):
            body = (
                "<!-- foreman:ready-head:head -->"
                if label == READY_FOR_REVIEW_LABEL
                else ""
            )
            status = dict(
                base,
                body=body,
                headRefOid="head",
                labels=[{"name": label}],
            )
            self.assertTrue(ready_for_review_now(gh, status), label)

        stale = dict(
            base,
            labels=[{"name": READY_FOR_REVIEW_LABEL}],
            statusCheckRollup=[{"status": "COMPLETED", "conclusion": "FAILURE"}],
        )
        self.assertFalse(ready_for_review_now(gh, stale))

        behind = dict(
            base,
            labels=[{"name": READY_FOR_REVIEW_LABEL}],
            mergeStateStatus="BEHIND",
            mergeable="MERGEABLE",
        )
        self.assertFalse(ready_for_review_now(gh, behind))

        awaiting_human_approval = dict(
            base,
            labels=[{"name": READY_FOR_REVIEW_LABEL}],
            mergeStateStatus="BLOCKED",
            mergeable="MERGEABLE",
        )
        self.assertTrue(ready_for_review_now(gh, awaiting_human_approval))

    def test_shepherd_writes_only_namespaced_ready_label(self):
        gh, _runner = make_github()
        status = {
            "number": 9,
            "title": "feat: unit 1",
            "url": "https://github.com/owner/repo/pull/9",
            "headRefName": "foreman/feat/1-unit",
            "headRefOid": "head",
            "isDraft": False,
            "labels": [],
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
            "mergeStateStatus": "CLEAN",
            "mergeable": "MERGEABLE",
        }
        gh.pr_status = lambda number: status  # type: ignore[method-assign]
        gh.review_threads = lambda number: []  # type: ignore[assignment]
        gh.promote_own_pr = lambda *args, **kwargs: (True, False)  # type: ignore[method-assign]
        gh.record_ready_head_own_pr = (  # type: ignore[method-assign]
            lambda number, *, expected_head_oid: (
                status.update(body=f"<!-- foreman:ready-head:{expected_head_oid} -->")
                or True
            )
        )
        writes = []
        gh.label_own_pr = (  # type: ignore[method-assign]
            lambda number, *, add=None, remove=None: (
                writes.append((number, add, remove))
                or status["labels"].extend({"name": name} for name in add or [])
            )
        )
        work = shepherd_pr(
            gh,
            Config(remote="origin"),
            Path("."),
            None,  # type: ignore[arg-type]
            {"number": 9, "_unit": 1},
            [],
        )
        self.assertEqual(work.state, "ready")
        self.assertEqual(writes, [(9, [READY_FOR_REVIEW_LABEL], None)])


class SharedReadinessGate(unittest.TestCase):
    def _status(self, merge_state: str, *, mergeable: str = "MERGEABLE") -> dict:
        return {
            "number": 9,
            "title": "feat: unit 1",
            "url": "https://github.com/owner/repo/pull/9",
            "state": "OPEN",
            "isDraft": False,
            "labels": [{"name": READY_FOR_REVIEW_LABEL}],
            "body": "<!-- foreman:ready-head:head -->",
            "headRefName": "foreman/feat/1-unit",
            "headRefOid": "head",
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
            "mergeStateStatus": merge_state,
            "mergeable": mergeable,
        }

    def test_shepherd_and_status_apply_the_same_live_verdict(self):
        cases = (
            ("CLEAN", "UNKNOWN", True),
            ("BLOCKED", "MERGEABLE", True),
            ("UNKNOWN", "MERGEABLE", False),
            ("UNSTABLE", "MERGEABLE", False),
        )
        for merge_state, mergeable, expected in cases:
            with self.subTest(merge_state=merge_state, mergeable=mergeable):
                gh, _runner = make_github()
                status = self._status(merge_state, mergeable=mergeable)
                gh.pr_status = lambda number, status=status: status  # type: ignore[method-assign]
                gh.review_threads = lambda number: []  # type: ignore[assignment]
                gh.promote_own_pr = lambda *args, **kwargs: (True, False)  # type: ignore[method-assign]
                gh.record_ready_head_own_pr = (  # type: ignore[method-assign]
                    lambda number, *, expected_head_oid, status=status: (
                        status.update(
                            body=f"<!-- foreman:ready-head:{expected_head_oid} -->"
                        )
                        or True
                    )
                )
                writes = []
                drafted = []
                gh.draft_own_pr = lambda number: drafted.append(number)  # type: ignore[method-assign]
                gh.label_own_pr = (  # type: ignore[method-assign]
                    lambda number, *, add=None, remove=None: (
                        writes.append((number, add, remove))
                        or status["labels"].extend({"name": name} for name in add or [])
                    )
                )

                work = shepherd_pr(
                    gh,
                    Config(remote="origin"),
                    Path("."),
                    None,  # type: ignore[arg-type]
                    {"number": 9, "_unit": 1},
                    [],
                )
                displayed_ready = ready_for_review_now(gh, status)

                self.assertEqual(work.state == "ready", expected)
                self.assertEqual(displayed_ready, expected)
                self.assertEqual(
                    any(add == [READY_FOR_REVIEW_LABEL] for _, add, _ in writes),
                    expected,
                )
                self.assertEqual(bool(drafted), not expected)

    def test_unstable_pending_checks_do_not_label_or_dispatch_a_fixer(self):
        gh, _runner = make_github()
        status = self._status("UNSTABLE")
        status["statusCheckRollup"] = [
            {
                "name": "non-required checks (mergeStateStatus UNSTABLE)",
                "status": "PENDING",
                "conclusion": "",
            }
        ]
        gh.pr_status = lambda number: status  # type: ignore[method-assign]
        writes = []
        drafted = []
        gh.draft_own_pr = lambda number: drafted.append(number)  # type: ignore[method-assign]
        gh.label_own_pr = (  # type: ignore[method-assign]
            lambda number, *, add=None, remove=None: writes.append(
                (number, add, remove)
            )
        )

        work = shepherd_pr(
            gh,
            Config(remote="origin"),
            Path("."),
            None,  # type: ignore[arg-type]
            {"number": 9, "_unit": 1},
            [],
        )

        self.assertEqual(work.state, "settling")
        self.assertEqual(work.actions, 0)
        self.assertEqual(drafted, [9])
        self.assertEqual(writes, [(9, None, [READY_FOR_REVIEW_LABEL])])

    def test_gate_rejects_unresolved_threads(self):
        status = self._status("CLEAN")
        self.assertFalse(
            automation_ready_now(
                status, lambda: [{"id": "thread", "isResolved": False}]
            )
        )


class DraftLifecycle(unittest.TestCase):
    def _status(
        self,
        *,
        head: str = "head",
        draft: bool = True,
        checks: str = "SUCCESS",
        labels: list[str] | None = None,
        merge_state: str | None = None,
    ) -> dict:
        return {
            "number": 9,
            "title": "feat: unit 1",
            "url": "https://github.com/owner/repo/pull/9",
            "state": "OPEN",
            "isDraft": draft,
            "labels": [{"name": label} for label in labels or []],
            "headRefName": "foreman/feat/1-unit",
            "headRefOid": head,
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": checks}],
            "mergeStateStatus": merge_state or ("DRAFT" if draft else "CLEAN"),
            "mergeable": "MERGEABLE",
        }

    def _shepherd(self, gh):
        return shepherd_pr(
            gh,
            Config(remote="origin"),
            Path("."),
            None,  # type: ignore[arg-type]
            {"number": 9, "_unit": 1},
            [],
        )

    def test_draft_is_promoted_only_after_post_transition_revalidation(self):
        gh, _runner = make_github()
        statuses = iter(
            [
                self._status(),
                self._status(),
                self._status(draft=False),
                dict(
                    self._status(draft=False, labels=[READY_FOR_REVIEW_LABEL]),
                    body="<!-- foreman:ready-head:head -->",
                ),
            ]
        )
        gh.pr_status = lambda number: next(statuses)  # type: ignore[method-assign]
        gh.review_threads = lambda number: []  # type: ignore[assignment]
        promotions = []
        gh.promote_own_pr = (  # type: ignore[method-assign]
            lambda number, *, expected_head_oid: (
                promotions.append((number, expected_head_oid)) or (True, True)
            )
        )
        gh.record_ready_head_own_pr = lambda *args, **kwargs: True  # type: ignore[method-assign]
        writes = []
        gh.label_own_pr = (  # type: ignore[method-assign]
            lambda number, *, add=None, remove=None: writes.append(
                (number, add, remove)
            )
        )

        work = self._shepherd(gh)

        self.assertEqual(work.state, "ready")
        self.assertIn("promoted", work.detail)
        self.assertEqual(promotions, [(9, "head")])
        self.assertEqual(writes, [(9, [READY_FOR_REVIEW_LABEL], None)])

    def test_configured_reviewer_blocks_both_shepherd_and_status_then_requests(self):
        gate_cfg = Config(
            remote="origin",
            reviewer_login="review-bot[bot]",
            reviewer_request="@review-bot review",
        )
        gh, _runner = make_github(gate_cfg)
        status = self._status()
        gh.pr_status = lambda number: status  # type: ignore[method-assign]
        gh.review_threads = lambda number: []  # type: ignore[assignment]
        gh.reviewer_evidence = lambda number: {  # type: ignore[method-assign]
            "viewer": "bot",
            "comments": [],
            "reviews": [],
        }
        requests = []
        gh.request_reviewer_own_pr = (  # type: ignore[method-assign]
            lambda number, *, expected_head_oid, body: (
                requests.append((number, expected_head_oid, body)) or True
            )
        )
        gh.promote_own_pr = lambda *args, **kwargs: self.fail("promoted")  # type: ignore[method-assign]

        work = shepherd_pr(
            gh,
            gate_cfg,
            Path("."),
            None,  # type: ignore[arg-type]
            {"number": 9, "_unit": 1},
            [],
        )
        displayed = dict(
            self._status(draft=False, labels=[READY_FOR_REVIEW_LABEL]),
            body="<!-- foreman:ready-head:head -->",
        )

        self.assertEqual(work.state, "settling")
        self.assertIn("attempt 1", work.detail)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][:2], (9, "head"))
        self.assertIn("@review-bot review", requests[0][2])
        self.assertFalse(ready_for_review_now(gh, displayed))

    def test_failed_checks_leave_draft_unpromoted(self):
        gh, _runner = make_github()
        gh.pr_status = lambda number: self._status(checks="FAILURE")  # type: ignore[method-assign]
        gh.review_threads = lambda number: []  # type: ignore[assignment]
        promoted = []
        gh.promote_own_pr = lambda *args, **kwargs: promoted.append(True)  # type: ignore[method-assign]

        ready, detail, _revealed = _promote_ready_head(gh, self._status())

        self.assertFalse(ready)
        self.assertIn("keeping PR in draft", detail)
        self.assertEqual(promoted, [])

    def test_unresolved_threads_leave_draft_unpromoted(self):
        gh, _runner = make_github()
        gh.pr_status = lambda number: self._status()  # type: ignore[method-assign]
        gh.review_threads = lambda number: [  # type: ignore[assignment]
            {"id": "thread", "isResolved": False}
        ]
        promoted = []
        gh.promote_own_pr = lambda *args, **kwargs: promoted.append(True)  # type: ignore[method-assign]

        ready, detail, _revealed = _promote_ready_head(gh, self._status())

        self.assertFalse(ready)
        self.assertIn("keeping PR in draft", detail)
        self.assertEqual(promoted, [])

    def test_missing_head_is_indeterminate_and_stays_draft(self):
        gh, _runner = make_github()
        status = self._status()
        status["headRefOid"] = ""
        ready, detail, _revealed = _promote_ready_head(gh, status)
        self.assertFalse(ready)
        self.assertIn("indeterminate", detail)

    def test_changed_head_during_promotion_is_returned_to_draft(self):
        gh, _runner = make_github()
        statuses = iter(
            [
                self._status(),
                self._status(),
                self._status(head="new-head", draft=False),
            ]
        )
        gh.pr_status = lambda number: next(statuses)  # type: ignore[method-assign]
        gh.review_threads = lambda number: []  # type: ignore[assignment]
        gh.promote_own_pr = lambda *args, **kwargs: (True, True)  # type: ignore[method-assign]
        gh.record_ready_head_own_pr = lambda *args, **kwargs: True  # type: ignore[method-assign]
        drafted = []
        gh.draft_own_pr = lambda number: drafted.append(number)  # type: ignore[method-assign]

        work = self._shepherd(gh)

        self.assertEqual(work.state, "settling")
        self.assertIn("returned PR to draft", work.detail)
        self.assertEqual(drafted, [9])

    def test_existing_non_draft_pr_uses_head_guard_without_transition(self):
        gh, _runner = make_github()
        status = self._status(draft=False)
        gh.pr_status = lambda number: status  # type: ignore[method-assign]
        gh.review_threads = lambda number: []  # type: ignore[assignment]
        promoted = []
        gh.promote_own_pr = (  # type: ignore[method-assign]
            lambda number, *, expected_head_oid: (
                promoted.append((number, expected_head_oid)) or (True, False)
            )
        )
        gh.record_ready_head_own_pr = (  # type: ignore[method-assign]
            lambda number, *, expected_head_oid: (
                status.update(body=f"<!-- foreman:ready-head:{expected_head_oid} -->")
                or True
            )
        )
        writes = []
        gh.label_own_pr = (  # type: ignore[method-assign]
            lambda number, *, add=None, remove=None: (
                writes.append((number, add, remove))
                or status["labels"].extend({"name": name} for name in add or [])
            )
        )

        work = self._shepherd(gh)

        self.assertEqual(work.state, "ready")
        self.assertEqual(promoted, [(9, "head")])
        self.assertEqual(writes, [(9, [READY_FOR_REVIEW_LABEL], None)])

    def test_already_draft_invalid_pr_loses_stale_readiness_label(self):
        gh, _runner = make_github()
        status = self._status(draft=True, labels=[READY_FOR_REVIEW_LABEL])
        status["statusCheckRollup"] = [{"status": "PENDING", "conclusion": ""}]
        gh.pr_status = lambda number: status  # type: ignore[method-assign]
        drafted = []
        gh.draft_own_pr = lambda number: drafted.append(number)  # type: ignore[method-assign]
        writes = []
        gh.label_own_pr = (  # type: ignore[method-assign]
            lambda number, *, add=None, remove=None: writes.append(
                (number, add, remove)
            )
        )

        work = self._shepherd(gh)

        self.assertEqual(work.state, "settling")
        self.assertEqual(drafted, [9])
        self.assertEqual(writes, [(9, None, [READY_FOR_REVIEW_LABEL])])

    def test_label_failure_after_promotion_returns_pr_to_draft(self):
        gh, _runner = make_github()
        statuses = iter([self._status(), self._status(draft=False)])
        gh.pr_status = lambda number: next(statuses)  # type: ignore[method-assign]
        gh.review_threads = lambda number: []  # type: ignore[assignment]
        gh.promote_own_pr = lambda *args, **kwargs: (True, True)  # type: ignore[method-assign]
        gh.record_ready_head_own_pr = lambda *args, **kwargs: True  # type: ignore[method-assign]
        gh.label_own_pr = (  # type: ignore[method-assign]
            lambda *args, **kwargs: (_ for _ in ()).throw(ForemanError("boom"))
        )
        drafted = []
        gh.draft_own_pr = lambda number: drafted.append(number)  # type: ignore[method-assign]

        with self.assertRaisesRegex(ForemanError, "boom"):
            _promote_ready_head(gh, self._status())

        self.assertEqual(drafted, [9])

    def test_promotion_exception_propagates_after_facade_compensation(self):
        gh, _runner = make_github()
        gh.pr_status = lambda number: self._status()  # type: ignore[method-assign]
        gh.review_threads = lambda number: []  # type: ignore[assignment]
        drafted = []
        gh.draft_own_pr = lambda number: drafted.append(number)  # type: ignore[method-assign]

        def failed_promotion(number, **_kwargs):
            gh.draft_own_pr(number)
            raise ForemanError("lost")

        gh.promote_own_pr = failed_promotion  # type: ignore[method-assign]
        gh.record_ready_head_own_pr = lambda *args, **kwargs: True  # type: ignore[method-assign]

        with self.assertRaisesRegex(ForemanError, "lost"):
            _promote_ready_head(gh, self._status())

        self.assertEqual(drafted, [9])

    def test_labeled_head_change_is_invalidated_during_fresh_read(self):
        gh, _runner = make_github()
        initial = self._status(draft=False, labels=[READY_FOR_REVIEW_LABEL])
        gh.pr_status = lambda number: self._status(  # type: ignore[method-assign]
            head="new", draft=False, labels=[READY_FOR_REVIEW_LABEL]
        )
        drafted = []
        gh.draft_own_pr = lambda number: drafted.append(number)  # type: ignore[method-assign]
        writes = []
        gh.label_own_pr = (  # type: ignore[method-assign]
            lambda number, *, add=None, remove=None: writes.append(
                (number, add, remove)
            )
        )

        ready, _detail, _revealed = _promote_ready_head(gh, initial)

        self.assertFalse(ready)
        self.assertEqual(drafted, [9])
        self.assertEqual(writes, [(9, None, [READY_FOR_REVIEW_LABEL])])

    def test_labeled_head_change_after_guard_is_returned_to_draft(self):
        gh, _runner = make_github()
        initial = dict(
            self._status(draft=False, labels=[READY_FOR_REVIEW_LABEL]),
            body="<!-- foreman:ready-head:head -->",
        )
        statuses = iter(
            [
                initial,
                dict(
                    self._status(
                        head="new", draft=False, labels=[READY_FOR_REVIEW_LABEL]
                    ),
                    body="<!-- foreman:ready-head:head -->",
                ),
            ]
        )
        gh.pr_status = lambda number: next(statuses)  # type: ignore[method-assign]
        gh.review_threads = lambda number: []  # type: ignore[assignment]
        gh.record_ready_head_own_pr = lambda *args, **kwargs: True  # type: ignore[method-assign]
        gh.promote_own_pr = lambda *args, **kwargs: (True, False)  # type: ignore[method-assign]
        drafted = []
        gh.draft_own_pr = lambda number: drafted.append(number)  # type: ignore[method-assign]
        writes = []
        gh.label_own_pr = (  # type: ignore[method-assign]
            lambda number, *, add=None, remove=None: writes.append(
                (number, add, remove)
            )
        )

        ready, detail, _revealed = _promote_ready_head(gh, initial)

        self.assertFalse(ready)
        self.assertIn("returned PR to draft", detail)
        self.assertEqual(drafted, [9])
        self.assertEqual(writes, [(9, None, [READY_FOR_REVIEW_LABEL])])

    def test_label_cleanup_is_attempted_when_redraft_fails(self):
        gh, _runner = make_github()
        status = self._status(draft=False, labels=[READY_FOR_REVIEW_LABEL])
        gh.draft_own_pr = lambda number: (_ for _ in ()).throw(  # type: ignore[method-assign]
            ForemanError("draft unavailable")
        )
        writes = []

        def fail_label_cleanup(number, *, add=None, remove=None):
            writes.append((number, add, remove))
            raise ForemanError("label cleanup unavailable")

        gh.label_own_pr = fail_label_cleanup  # type: ignore[method-assign]

        with self.assertRaisesRegex(ForemanError, "draft unavailable"):
            _return_to_draft(gh, status)

        self.assertEqual(writes, [(9, None, [READY_FOR_REVIEW_LABEL])])

    def test_green_push_invalidates_persisted_ready_head_for_one_tick(self):
        gh, _runner = make_github()
        status = self._status(draft=False, labels=[READY_FOR_REVIEW_LABEL])
        status["body"] = "<!-- foreman:ready-head:old -->"
        gh.pr_status = lambda number: status  # type: ignore[method-assign]
        drafted = []
        gh.draft_own_pr = lambda number: drafted.append(number)  # type: ignore[method-assign]
        writes = []
        gh.label_own_pr = (  # type: ignore[method-assign]
            lambda number, *, add=None, remove=None: writes.append(
                (number, add, remove)
            )
        )
        gh.promote_own_pr = lambda *args, **kwargs: self.fail("re-promoted")  # type: ignore[method-assign]
        gh.review_threads = lambda number: []  # type: ignore[assignment]

        work = self._shepherd(gh)

        self.assertEqual(work.state, "settling")
        self.assertEqual(drafted, [9])
        self.assertEqual(writes, [(9, None, [READY_FOR_REVIEW_LABEL])])

    def test_pre_marker_ready_pr_is_migrated_without_demotion(self):
        gh, _runner = make_github()
        status = self._status(draft=False, labels=[READY_FOR_REVIEW_LABEL])
        gh.review_threads = lambda number: []  # type: ignore[assignment]
        recorded = []
        gh.record_ready_head_own_pr = (  # type: ignore[method-assign]
            lambda number, *, expected_head_oid: (
                recorded.append((number, expected_head_oid)) or True
            )
        )
        gh.draft_own_pr = lambda number: self.fail("demoted")  # type: ignore[method-assign]

        self.assertFalse(_demote_promoted_if_invalid(gh, status))
        self.assertEqual(recorded, [(9, "head")])

    def test_push_during_final_writes_is_returned_to_draft(self):
        gh, _runner = make_github()
        statuses = iter(
            [
                self._status(),
                self._status(draft=False),
                dict(
                    self._status(
                        head="new", draft=False, labels=[READY_FOR_REVIEW_LABEL]
                    ),
                    body="<!-- foreman:ready-head:head -->",
                ),
            ]
        )
        gh.pr_status = lambda number: next(statuses)  # type: ignore[method-assign]
        gh.review_threads = lambda number: []  # type: ignore[assignment]
        gh.promote_own_pr = lambda *args, **kwargs: (True, True)  # type: ignore[method-assign]
        gh.record_ready_head_own_pr = lambda *args, **kwargs: True  # type: ignore[method-assign]
        drafted = []
        gh.draft_own_pr = lambda number: drafted.append(number)  # type: ignore[method-assign]
        writes = []
        gh.label_own_pr = (  # type: ignore[method-assign]
            lambda number, *, add=None, remove=None: writes.append(
                (number, add, remove)
            )
        )

        ready, detail, _revealed = _promote_ready_head(gh, self._status())

        self.assertFalse(ready)
        self.assertIn("final writes", detail)
        self.assertEqual(drafted, [9])
        self.assertEqual(
            writes,
            [
                (9, [READY_FOR_REVIEW_LABEL], None),
                (9, None, [READY_FOR_REVIEW_LABEL]),
            ],
        )

    def test_interrupted_promotion_with_invalid_evidence_is_recovered(self):
        gh, _runner = make_github()
        status = dict(
            self._status(draft=False, checks="FAILURE"),
            body="<!-- foreman:ready-head:head -->",
        )
        drafted = []
        gh.draft_own_pr = lambda number: drafted.append(number)  # type: ignore[method-assign]

        self.assertTrue(_recover_interrupted_promotion(gh, status))
        self.assertEqual(drafted, [9])

    def test_interrupted_promotion_thread_read_failure_returns_to_draft(self):
        gh, _runner = make_github()
        status = dict(
            self._status(draft=False),
            body="<!-- foreman:ready-head:head -->",
        )
        gh.review_threads = lambda number: (_ for _ in ()).throw(  # type: ignore[assignment]
            ForemanError("threads unavailable")
        )
        drafted = []
        gh.draft_own_pr = lambda number: drafted.append(number)  # type: ignore[method-assign]

        with self.assertRaisesRegex(ForemanError, "threads unavailable"):
            _recover_interrupted_promotion(gh, status)

        self.assertEqual(drafted, [9])

    def test_initial_status_failure_demotes_from_pr_list_snapshot(self):
        gh, _runner = make_github()
        gh.pr_status = lambda number: (_ for _ in ()).throw(  # type: ignore[method-assign]
            ForemanError("checks unavailable")
        )
        drafted = []
        gh.draft_own_pr = lambda number: drafted.append(number)  # type: ignore[method-assign]
        writes = []
        gh.label_own_pr = (  # type: ignore[method-assign]
            lambda number, *, add=None, remove=None: writes.append(
                (number, add, remove)
            )
        )
        pr = {
            "number": 9,
            "_unit": 1,
            "isDraft": False,
            "body": "<!-- foreman:ready-head:head -->",
            "labels": [{"name": READY_FOR_REVIEW_LABEL}],
        }

        with self.assertRaisesRegex(ForemanError, "checks unavailable"):
            shepherd_pr(
                gh,
                Config(remote="origin"),
                Path("."),
                None,  # type: ignore[arg-type]
                pr,
                [],
            )

        self.assertEqual(drafted, [9])
        self.assertEqual(writes, [(9, None, [READY_FOR_REVIEW_LABEL])])

    def test_conflicting_draft_routes_directly_to_merge_repair(self):
        gh, _runner = make_github()
        status = self._status()
        status["mergeable"] = "CONFLICTING"
        gh.pr_status = lambda number: status  # type: ignore[method-assign]

        def repaired(_gh, _cfg, _root, _selection, work):
            work.state, work.detail = "rebased", "draft conflict repaired"
            return work

        with patch(
            "foreman.shepherd._repair_merge_state", side_effect=repaired
        ) as repair:
            work = self._shepherd(gh)

        self.assertEqual(work.state, "rebased")
        repair.assert_called_once()

    def test_draft_that_reveals_behind_is_routed_to_rebase(self):
        gh, _runner = make_github()
        statuses = iter(
            [
                self._status(),
                self._status(),
                self._status(draft=False, merge_state="BEHIND"),
            ]
        )
        gh.pr_status = lambda number: next(statuses)  # type: ignore[method-assign]
        gh.review_threads = lambda number: []  # type: ignore[assignment]
        gh.promote_own_pr = lambda *args, **kwargs: (True, True)  # type: ignore[method-assign]
        gh.record_ready_head_own_pr = lambda *args, **kwargs: True  # type: ignore[method-assign]
        drafted = []
        gh.draft_own_pr = lambda number: drafted.append(number)  # type: ignore[method-assign]

        def repaired(_gh, _cfg, _root, _selection, work):
            work.state, work.detail = "rebased", "hidden behind state repaired"
            return work

        with patch(
            "foreman.shepherd._repair_merge_state", side_effect=repaired
        ) as repair:
            work = self._shepherd(gh)

        self.assertEqual(work.state, "rebased")
        self.assertEqual(drafted, [9])
        repair.assert_called_once()


class ReviewThreadReads(unittest.TestCase):
    """#54: unread threads must never read as resolved — that path ends in
    ready-for-review."""

    def _resp(self, total, nodes):
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {"totalCount": total, "nodes": nodes}
                    }
                }
            }
        }

    def test_more_threads_than_fetched_fails_closed(self):
        gh, runner = make_github()
        runner.when(
            ["api", "graphql"], self._resp(101, [{"id": "t1", "isResolved": False}])
        )
        with self.assertRaises(ForemanError):
            gh.review_threads(9)

    def test_unreadable_response_fails_closed(self):
        gh, runner = make_github()
        runner.when(["api", "graphql"], {"data": None})
        with self.assertRaises(ForemanError):
            gh.review_threads(9)

    def test_partial_metadata_fails_closed(self):
        # nodes: null / totalCount: null is not an empty complete page.
        gh, runner = make_github()
        runner.when(["api", "graphql"], self._resp(None, None))
        with self.assertRaises(ForemanError):
            gh.review_threads(9)

    def test_complete_page_is_returned(self):
        gh, runner = make_github()
        nodes = [{"id": "t1", "isResolved": True}]
        runner.when(["api", "graphql"], self._resp(1, nodes))
        self.assertEqual(gh.review_threads(9), nodes)


class ClassifyChecks(unittest.TestCase):
    def test_all_green(self):
        rollup = [
            {"status": "COMPLETED", "conclusion": "SUCCESS"},
            {"status": "COMPLETED", "conclusion": "SKIPPED"},
            {"status": "COMPLETED", "conclusion": "NEUTRAL"},
        ]
        state, failed = classify_checks(rollup)
        self.assertEqual(state, "green")
        self.assertEqual(failed, [])

    def test_failure_wins_over_pending(self):
        rollup = [
            {"status": "IN_PROGRESS", "conclusion": ""},
            {"status": "COMPLETED", "conclusion": "FAILURE", "name": "verify"},
        ]
        state, failed = classify_checks(rollup)
        self.assertEqual(state, "red")
        self.assertEqual(failed[0]["name"], "verify")

    def test_pending_when_running(self):
        state, _ = classify_checks([{"status": "QUEUED", "conclusion": ""}])
        self.assertEqual(state, "pending")

    def test_empty_rollup_is_green(self):
        self.assertEqual(classify_checks([])[0], "green")
        self.assertEqual(classify_checks(None)[0], "green")

    def test_legacy_status_contexts(self):
        state, failed = classify_checks([{"state": "FAILURE", "context": "ci/legacy"}])
        self.assertEqual(state, "red")
        self.assertEqual(failed[0]["context"], "ci/legacy")


class SignatureCatalog(unittest.TestCase):
    def setUp(self):
        self.catalog = signatures_mod.load()

    def test_seeded_environment_signatures(self):
        sig = signatures_mod.match(
            "Error: DeploymentQuotaReached for team", self.catalog
        )
        self.assertIsNotNone(sig)
        self.assertEqual(sig.action, "environment")
        sig = signatures_mod.match(
            "The job was not started because recent account payments have failed",
            self.catalog,
        )
        self.assertEqual(sig.action, "environment")

    def test_docker_daemon_signature(self):
        # The #29 pickup: a docker-keyed check that only Actions ran, dying
        # on daemon absence, is classified environmental before any LLM
        # sees it — never handed to an agent as a code bug.
        sig = signatures_mod.match(
            "docker: Cannot connect to the Docker daemon at "
            "unix:///var/run/docker.sock. Is the docker daemon running?",
            self.catalog,
        )
        self.assertIsNotNone(sig)
        self.assertEqual(sig.name, "docker-daemon-unavailable")
        self.assertEqual(sig.action, "environment")

    def test_quota_wait_signature(self):
        sig = signatures_mod.match(
            "You have hit your usage limit. Limit will reset at 3pm", self.catalog
        )
        self.assertIsNotNone(sig)
        self.assertEqual(sig.action, "quota_wait")

    def test_no_match_returns_none(self):
        self.assertIsNone(
            signatures_mod.match("TypeError: x is not a function", self.catalog)
        )


if __name__ == "__main__":
    unittest.main()


class DerivedCheckContexts(unittest.TestCase):
    """#89: fine-grained PATs cannot read the GraphQL rollup; CI state is
    derived from Actions runs + combined commit status, in rollup shape."""

    def _gh(self, workflow_runs, statuses):
        gh, runner = _mk()
        runner.when(
            ["pr", "view", "9"],
            {"number": 9, "headRefOid": "abc123", "labels": []},
        )
        runner.when(
            ["api", "repos/owner/repo/actions/runs?head_sha=abc123&per_page=100"],
            [{"workflow_runs": workflow_runs}],
        )
        for wf in workflow_runs:
            rid = wf.get("id", 0)
            jobs = wf.get("jobs")
            if jobs is None:
                jobs = [
                    {
                        "name": wf.get("name"),
                        "status": wf.get("status"),
                        "conclusion": wf.get("conclusion"),
                    }
                ]
            runner.when(
                [
                    "api",
                    f"repos/owner/repo/actions/runs/{rid}/jobs?filter=all&per_page=100",
                ],
                [{"jobs": jobs}],
            )
        runner.when(
            ["api", "repos/owner/repo/commits/abc123/status?per_page=100"],
            [{"statuses": statuses}],
        )
        runner.when(["api", "repos/owner/repo/rules/branches/main?per_page=100"], [[]])
        return gh

    def test_actions_and_commit_status_synthesize_rollup(self):
        gh = self._gh(
            [
                {"name": "build", "status": "completed", "conclusion": "success"},
                {"name": "e2e", "status": "in_progress", "conclusion": None},
            ],
            [{"context": "vendor/scan", "state": "failure"}],
        )
        rollup = gh.pr_status(9)["statusCheckRollup"]
        state, failed = classify_checks(rollup)
        self.assertEqual(state, "red")
        self.assertEqual([f["name"] for f in failed], ["vendor/scan"])

    def test_all_green_and_pending_bucketing(self):
        gh = self._gh(
            [{"name": "build", "status": "completed", "conclusion": "success"}],
            [],
        )
        self.assertEqual(
            classify_checks(gh.pr_status(9)["statusCheckRollup"])[0], "green"
        )
        gh = self._gh([{"name": "build", "status": "queued", "conclusion": None}], [])
        self.assertEqual(
            classify_checks(gh.pr_status(9)["statusCheckRollup"])[0], "pending"
        )

    def test_fixture_supplied_rollup_is_honored(self):
        gh, runner = _mk()
        runner.when(
            ["pr", "view", "9"],
            {"number": 9, "statusCheckRollup": [{"conclusion": "FAILURE"}]},
        )
        self.assertEqual(
            classify_checks(gh.pr_status(9)["statusCheckRollup"])[0], "red"
        )


class CheckContextEdgeCases(unittest.TestCase):
    """Round-two hardening of the derived rollup (#89)."""

    def _gh(self, workflow_runs, statuses, rules=None):
        gh, runner = _mk()
        runner.when(
            ["pr", "view", "9"],
            {"number": 9, "headRefOid": "abc123", "baseRefName": "main"},
        )
        runner.when(
            ["api", "repos/owner/repo/actions/runs?head_sha=abc123&per_page=100"],
            [{"workflow_runs": workflow_runs}],
        )
        for wf in workflow_runs:
            rid = wf.get("id", 0)
            jobs = wf.get("jobs")
            if jobs is None:
                jobs = [
                    {
                        "name": wf.get("name"),
                        "status": wf.get("status"),
                        "conclusion": wf.get("conclusion"),
                    }
                ]
            runner.when(
                [
                    "api",
                    f"repos/owner/repo/actions/runs/{rid}/jobs?filter=all&per_page=100",
                ],
                [{"jobs": jobs}],
            )
        runner.when(
            ["api", "repos/owner/repo/commits/abc123/status?per_page=100"],
            [{"statuses": statuses}],
        )
        runner.when(
            ["api", "repos/owner/repo/rules/branches/main?per_page=100"],
            [rules or []],
        )
        return gh

    def test_unknown_conclusion_normalizes_to_failure(self):
        gh = self._gh(
            [
                {
                    "name": "build",
                    "status": "completed",
                    "conclusion": "startup_failure",
                }
            ],
            [],
        )
        state, failed = classify_checks(gh.pr_status(9)["statusCheckRollup"])
        self.assertEqual(state, "red")
        self.assertEqual(failed[0]["conclusion"], "FAILURE")

    def test_superseded_run_is_ignored(self):
        gh = self._gh(
            [
                {
                    "id": 1,
                    "name": "build",
                    "status": "completed",
                    "conclusion": "failure",
                },
                {
                    "id": 2,
                    "name": "build",
                    "status": "completed",
                    "conclusion": "success",
                },
            ],
            [],
        )
        self.assertEqual(
            classify_checks(gh.pr_status(9)["statusCheckRollup"])[0], "green"
        )

    def test_missing_required_context_is_pending_never_green(self):
        gh = self._gh(
            [{"name": "build", "status": "completed", "conclusion": "success"}],
            [],
            rules=[
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "required_status_checks": [{"context": "third-party/scan"}]
                    },
                }
            ],
        )
        rollup = gh.pr_status(9)["statusCheckRollup"]
        self.assertEqual(classify_checks(rollup)[0], "pending")
        self.assertTrue(any("third-party/scan" in c["name"] for c in rollup))


class JobGranularity(unittest.TestCase):
    """#89 round three: required contexts name Actions JOBS, and
    event-distinct runs are separate check identities."""

    _gh = CheckContextEdgeCases._gh

    def test_required_context_matches_job_name(self):
        gh = self._gh(
            [
                {
                    "id": 5,
                    "name": "Build & Validate",
                    "status": "completed",
                    "conclusion": "success",
                    "jobs": [
                        {
                            "name": "verify",
                            "status": "completed",
                            "conclusion": "success",
                        },
                        {
                            "name": "security",
                            "status": "completed",
                            "conclusion": "success",
                        },
                    ],
                }
            ],
            [],
            rules=[
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "required_status_checks": [
                            {"context": "verify"},
                            {"context": "security"},
                        ]
                    },
                }
            ],
        )
        rollup = gh.pr_status(9)["statusCheckRollup"]
        self.assertEqual(classify_checks(rollup)[0], "green")
        self.assertFalse(any("unobservable" in c["name"] for c in rollup))

    def test_event_distinct_runs_both_count(self):
        gh = self._gh(
            [
                {
                    "id": 1,
                    "name": "Build & Validate",
                    "event": "pull_request",
                    "status": "completed",
                    "conclusion": "failure",
                },
                {
                    "id": 2,
                    "name": "Build & Validate",
                    "event": "workflow_dispatch",
                    "status": "completed",
                    "conclusion": "success",
                },
            ],
            [],
        )
        self.assertEqual(
            classify_checks(gh.pr_status(9)["statusCheckRollup"])[0], "red"
        )


class IntegrationBoundRequirements(unittest.TestCase):
    """#89 round four: a required check bound to an integration is not
    satisfied by a same-named observation from another source."""

    _gh = CheckContextEdgeCases._gh

    def test_status_context_cannot_satisfy_actions_bound_requirement(self):
        gh = self._gh(
            [],
            [{"context": "verify", "state": "success"}],
            rules=[
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "required_status_checks": [
                            {"context": "verify", "integration_id": 15368}
                        ]
                    },
                }
            ],
        )
        rollup = gh.pr_status(9)["statusCheckRollup"]
        self.assertEqual(classify_checks(rollup)[0], "pending")
        self.assertTrue(any("unobservable" in c["name"] for c in rollup))

    def test_partial_rerun_keeps_prior_successful_jobs(self):
        gh = self._gh(
            [
                {
                    "id": 7,
                    "name": "Build & Validate",
                    "status": "completed",
                    "conclusion": "success",
                    "jobs": [
                        {
                            "name": "verify",
                            "status": "completed",
                            "conclusion": "success",
                            "run_attempt": 1,
                            "id": 11,
                        },
                        {
                            "name": "security",
                            "status": "completed",
                            "conclusion": "failure",
                            "run_attempt": 1,
                            "id": 12,
                        },
                        {
                            "name": "security",
                            "status": "completed",
                            "conclusion": "success",
                            "run_attempt": 2,
                            "id": 20,
                        },
                    ],
                }
            ],
            [],
        )
        self.assertEqual(
            classify_checks(gh.pr_status(9)["statusCheckRollup"])[0], "green"
        )


class RunAttemptAndIdentity(unittest.TestCase):
    """#89 round four: run status is authoritative during reruns, workflow
    identity is the stable workflow_id, same-attempt name collisions are
    retained, and a red run never launders green through job filtering."""

    _gh = CheckContextEdgeCases._gh

    def test_rerunning_workflow_masks_prior_attempt_jobs(self):
        # filter=all serves attempt-1 jobs while attempt 2 is in flight;
        # neither their stale green nor stale red may leak.
        for stale in ("success", "failure"):
            gh = self._gh(
                [
                    {
                        "id": 7,
                        "name": "CI",
                        "status": "in_progress",
                        "conclusion": None,
                        "jobs": [
                            {
                                "name": "verify",
                                "status": "completed",
                                "conclusion": stale,
                                "run_attempt": 1,
                                "id": 11,
                            }
                        ],
                    }
                ],
                [],
            )
            self.assertEqual(
                classify_checks(gh.pr_status(9)["statusCheckRollup"])[0],
                "pending",
                f"stale {stale} attempt-1 job leaked through a live rerun",
            )

    def test_same_display_name_distinct_workflow_files_both_count(self):
        gh = self._gh(
            [
                {
                    "id": 5,
                    "workflow_id": 200,
                    "name": "CI",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "failure",
                },
                {
                    "id": 9,
                    "workflow_id": 100,
                    "name": "CI",
                    "event": "push",
                    "status": "completed",
                    "conclusion": "success",
                },
            ],
            [],
        )
        self.assertEqual(
            classify_checks(gh.pr_status(9)["statusCheckRollup"])[0], "red"
        )

    def test_same_attempt_name_collision_keeps_the_red_sibling(self):
        gh = self._gh(
            [
                {
                    "id": 7,
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "failure",
                    "jobs": [
                        {
                            "name": "build",
                            "status": "completed",
                            "conclusion": "success",
                            "run_attempt": 1,
                            "id": 10,
                        },
                        {
                            "name": "build",
                            "status": "completed",
                            "conclusion": "failure",
                            "run_attempt": 1,
                            "id": 11,
                        },
                    ],
                }
            ],
            [],
        )
        self.assertEqual(
            classify_checks(gh.pr_status(9)["statusCheckRollup"])[0], "red"
        )

    def test_red_run_with_green_visible_jobs_stays_red(self):
        # The failing job fell outside the attempt view; the run verdict wins.
        gh = self._gh(
            [
                {
                    "id": 7,
                    "name": "CI",
                    "status": "completed",
                    "conclusion": "failure",
                    "jobs": [
                        {
                            "name": "build",
                            "status": "completed",
                            "conclusion": "success",
                            "run_attempt": 2,
                            "id": 20,
                        }
                    ],
                }
            ],
            [],
        )
        self.assertEqual(
            classify_checks(gh.pr_status(9)["statusCheckRollup"])[0], "red"
        )


class MergeStateAndBranchEncoding(unittest.TestCase):
    """#89 round four: UNSTABLE surfaces invisible optional-check failures,
    and slash-containing base branches reach the rules endpoint encoded."""

    def test_unstable_merge_state_never_classifies_green(self):
        gh, runner = _mk()
        runner.when(
            ["pr", "view", "9"],
            {
                "number": 9,
                "headRefOid": "abc123",
                "baseRefName": "main",
                "mergeStateStatus": "UNSTABLE",
            },
        )
        runner.when(
            ["api", "repos/owner/repo/actions/runs?head_sha=abc123&per_page=100"],
            [{"workflow_runs": []}],
        )
        runner.when(
            ["api", "repos/owner/repo/commits/abc123/status?per_page=100"],
            [{"statuses": [{"context": "lint", "state": "success"}]}],
        )
        runner.when(["api", "repos/owner/repo/rules/branches/main?per_page=100"], [[]])
        rollup = gh.pr_status(9)["statusCheckRollup"]
        self.assertEqual(classify_checks(rollup)[0], "pending")
        self.assertTrue(any("UNSTABLE" in c["name"] for c in rollup))

    def test_slash_branch_is_encoded_in_rules_path(self):
        gh, runner = _mk()
        # FakeRunner raises on any unexpected argv: passing proves the
        # branch rode the path as one percent-encoded segment.
        runner.when(
            ["api", "repos/owner/repo/rules/branches/release%2F2.x?per_page=100"],
            [[]],
        )
        self.assertEqual(gh._required_contexts("release/2.x"), [])


class ProvenanceGate(unittest.TestCase):
    def test_foreign_pr_escalation_leaves_no_issue_provenance(self):
        # #82: a foreign PR wearing the label + a forged unit marker names an
        # attacker-chosen issue; its escalation must not write status events.
        import tempfile
        from pathlib import Path

        from foreman import shepherd as shepherd_mod
        from foreman.config import Config

        gh, runner = make_github()
        pr_fields = {
            "state": "OPEN",
            "isDraft": False,
            "baseRefName": "main",
            "labels": [],
        }
        runner.when(
            ["pr", "list"],
            [
                {
                    "number": 30,
                    "title": "own",
                    "body": "<!-- foreman:unit=#7 -->",
                    "url": "u30",
                    "headRefName": "b7",
                    "author": {"login": "bot"},
                    **pr_fields,
                },
                {
                    "number": 31,
                    "title": "forged",
                    "body": "<!-- foreman:unit=#9 -->",
                    "url": "u31",
                    "headRefName": "b9",
                    "author": {"login": "mallory"},
                    **pr_fields,
                },
            ],
        )
        runner.when(["label", "create"], "")
        runner.when(
            ["api", "repos/owner/repo/issues/7/comments", "--paginate", "--slurp"],
            [[]],
        )
        runner.when(["api", "--method", "POST"], "{}")

        def raise_guard(*args, **kwargs):
            raise ForemanError("own-PR guard")

        original = shepherd_mod.shepherd_pr
        shepherd_mod.shepherd_pr = raise_guard
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = shepherd_mod.run_shepherd(gh, Config(), Path(tmp), None)
        finally:
            shepherd_mod.shepherd_pr = original

        # Both escalations are reported to the operator...
        self.assertEqual(sorted(out.environmental), [7, 9])
        creates = runner.called_with_prefix(["label", "create"])
        self.assertEqual(
            {argv[2] for argv in creates},
            {DISPATCHED_LABEL, READY_FOR_REVIEW_LABEL},
        )
        # ...but only foreman's own PR leaves provenance on its issue.
        posts = runner.called_with_prefix(["api", "--method", "POST"])
        self.assertEqual(len(posts), 1)
        self.assertIn("issues/7/comments", posts[0][3])
        touched_9 = [
            argv for argv, _ in runner.calls if any("issues/9/" in a for a in argv)
        ]
        self.assertEqual(touched_9, [])
