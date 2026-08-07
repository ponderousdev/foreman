"""Shepherd classification: check-rollup bucketing and the signature catalog."""

from __future__ import annotations

import unittest
from pathlib import Path

from foreman import signatures as signatures_mod
from foreman.config import Config
from foreman.github import (
    DISPATCHED_LABEL,
    LEGACY_DISPATCHED_LABEL,
    LEGACY_READY_FOR_REVIEW_LABEL,
    READY_FOR_REVIEW_LABEL,
)
from foreman.shepherd import (
    classify_checks,
    open_foreman_prs,
    ready_for_review_now,
    shepherd_pr,
)
from foreman.util import ForemanError
from tests.fakes import make_github, pr_json
from tests.fakes import make_github as _mk


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
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
            "mergeStateStatus": "CLEAN",
        }
        for label in (READY_FOR_REVIEW_LABEL, LEGACY_READY_FOR_REVIEW_LABEL):
            status = dict(base, labels=[{"name": label}])
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
        gh.pr_status = lambda number: {  # type: ignore[method-assign]
            "number": number,
            "title": "feat: unit 1",
            "url": "https://github.com/owner/repo/pull/9",
            "headRefName": "foreman/feat/1-unit",
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
            "mergeStateStatus": "CLEAN",
            "mergeable": "MERGEABLE",
        }
        gh.review_threads = lambda number: []  # type: ignore[assignment]
        writes = []
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
        self.assertEqual(work.state, "ready")
        self.assertEqual(writes, [(9, [READY_FOR_REVIEW_LABEL], None)])


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
