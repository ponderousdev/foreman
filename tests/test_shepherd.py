"""Shepherd classification: check-rollup bucketing and the signature catalog."""

from __future__ import annotations

import unittest

from foreman import signatures as signatures_mod
from foreman.shepherd import classify_checks
from foreman.util import ForemanError
from tests.fakes import make_github
from tests.fakes import make_github as _mk


class ReviewThreadReads(unittest.TestCase):
    """#54: unread threads must never read as resolved — that path ends in
    ready-to-merge."""

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
        runner.when(
            ["api", "repos/owner/repo/commits/abc123/status"],
            {"statuses": statuses},
        )
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
