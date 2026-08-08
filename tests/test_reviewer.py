"""Provider-neutral exact-head reviewer evidence and bounded requests."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from foreman.config import Config
from foreman.reviewer import request_body, request_marker, reviewer_verdict
from foreman.util import ForemanError
from tests.fakes import make_github

HEAD = "a" * 40
OLD_HEAD = "b" * 40
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def cfg(**changes) -> Config:
    values = {
        "reviewer_login": "review-bot[bot]",
        "reviewer_request": "@review-bot review",
        "reviewer_timeout_min": 10,
        "reviewer_max_attempts": 2,
    }
    values.update(changes)
    return Config(**values)


def review(
    *,
    head: str = HEAD,
    state: str = "COMMENTED",
    comments: int = 0,
    submitted: datetime = NOW,
) -> dict:
    return {
        "author": {"login": "review-bot[bot]"},
        "commit": {"oid": head},
        "state": state,
        "submittedAt": submitted.isoformat(),
        "comments": {"totalCount": comments},
    }


def request(
    *,
    head: str = HEAD,
    attempt: int = 1,
    created: datetime | None = None,
    reactions: list[dict] | None = None,
    author: str = "bot",
) -> dict:
    return {
        "author": {"login": author},
        "body": request_marker(head, attempt),
        "createdAt": (created or NOW).isoformat(),
        "reactions": {"nodes": reactions or []},
    }


def reaction(content: str, login: str = "review-bot[bot]") -> dict:
    return {"content": content, "user": {"login": login}}


def evidence(*, comments=None, reviews=None) -> dict:
    return {"viewer": "bot", "comments": comments or [], "reviews": reviews or []}


class ReviewerState(unittest.TestCase):
    def test_disabled_gate_is_ready_without_evidence(self):
        verdict = reviewer_verdict(Config(), HEAD, {})
        self.assertEqual(verdict.state, "disabled")
        self.assertTrue(verdict.ready)

    def test_missing_and_stale_evidence_request_attempt_one(self):
        missing = reviewer_verdict(cfg(), HEAD, evidence(), now=NOW)
        stale = reviewer_verdict(
            cfg(), HEAD, evidence(comments=[request(head=OLD_HEAD)]), now=NOW
        )
        self.assertEqual((missing.state, missing.next_attempt), ("missing", 1))
        self.assertEqual((stale.state, stale.next_attempt), ("stale", 1))

    def test_approved_review_succeeds_only_for_exact_head(self):
        accepted = reviewer_verdict(
            cfg(), HEAD, evidence(reviews=[review(state="APPROVED")]), now=NOW
        )
        stale = reviewer_verdict(
            cfg(),
            HEAD,
            evidence(reviews=[review(head=OLD_HEAD, state="APPROVED")]),
            now=NOW,
        )
        self.assertEqual(accepted.state, "success")
        self.assertTrue(accepted.ready)
        self.assertEqual(stale.state, "stale")

    def test_success_reaction_is_bound_to_latest_request(self):
        old = request(
            attempt=1,
            created=NOW - timedelta(minutes=2),
            reactions=[reaction("THUMBS_UP")],
        )
        latest = request(attempt=2, created=NOW - timedelta(minutes=1))
        pending = reviewer_verdict(
            cfg(reviewer_max_attempts=3),
            HEAD,
            evidence(comments=[old, latest]),
            now=NOW,
        )
        latest["reactions"]["nodes"] = [reaction("THUMBS_UP")]
        accepted = reviewer_verdict(
            cfg(reviewer_max_attempts=3),
            HEAD,
            evidence(comments=[old, latest]),
            now=NOW,
        )
        self.assertEqual(pending.state, "pending")
        self.assertEqual(accepted.state, "success")

    def test_findings_retry_then_stop_at_bound(self):
        first = reviewer_verdict(
            cfg(),
            HEAD,
            evidence(
                comments=[request(attempt=1, created=NOW - timedelta(minutes=1))],
                reviews=[review(comments=1)],
            ),
            now=NOW,
        )
        second = reviewer_verdict(
            cfg(),
            HEAD,
            evidence(
                comments=[request(attempt=2, created=NOW - timedelta(minutes=1))],
                reviews=[review(comments=1)],
            ),
            now=NOW,
        )
        self.assertEqual((first.state, first.next_attempt), ("findings", 2))
        self.assertEqual((second.state, second.next_attempt), ("findings", None))

    def test_dismissed_findings_and_pre_request_reviews_do_not_count(self):
        latest = request(
            created=NOW - timedelta(minutes=1),
            author="BOT",
        )
        verdict = reviewer_verdict(
            cfg(),
            HEAD,
            evidence(
                comments=[latest],
                reviews=[
                    review(state="DISMISSED", comments=1),
                    review(comments=1, submitted=NOW - timedelta(minutes=2)),
                ],
            ),
            now=NOW,
        )
        self.assertEqual(verdict.state, "pending")

    def test_contradictory_success_and_findings_fail_closed(self):
        verdict = reviewer_verdict(
            cfg(),
            HEAD,
            evidence(
                comments=[
                    request(
                        created=NOW - timedelta(minutes=1),
                        reactions=[reaction("THUMBS_UP")],
                    )
                ],
                reviews=[review(comments=1)],
            ),
            now=NOW,
        )
        self.assertEqual(verdict.state, "contradictory")
        self.assertFalse(verdict.ready)
        self.assertFalse(verdict.can_request)

    def test_pending_timeout_retry_and_terminal_timeout(self):
        recent = request(created=NOW - timedelta(minutes=9))
        old_first = request(created=NOW - timedelta(minutes=10))
        old_last = request(attempt=2, created=NOW - timedelta(minutes=10))
        pending = reviewer_verdict(cfg(), HEAD, evidence(comments=[recent]), now=NOW)
        retry = reviewer_verdict(cfg(), HEAD, evidence(comments=[old_first]), now=NOW)
        terminal = reviewer_verdict(cfg(), HEAD, evidence(comments=[old_last]), now=NOW)
        self.assertEqual(pending.state, "pending")
        self.assertEqual((retry.state, retry.next_attempt), ("timed_out", 2))
        self.assertEqual((terminal.state, terminal.next_attempt), ("timed_out", None))

    def test_unreadable_evidence_is_indeterminate(self):
        verdict = reviewer_verdict(cfg(), HEAD, {"comments": [], "reviews": []})
        self.assertEqual(verdict.state, "indeterminate")

    def test_request_text_is_opaque_and_marker_bound(self):
        body = request_body(cfg(), HEAD, 2)
        self.assertTrue(body.startswith("@review-bot review\n"))
        self.assertIn(f"head={HEAD} attempt=2", body)


class ReviewerEvidenceRead(unittest.TestCase):
    def response(self, *, reaction_total: int = 0, review_total: int = 0) -> dict:
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "comments": {
                            "totalCount": 1,
                            "nodes": [
                                {
                                    "body": request_marker(HEAD, 1),
                                    "createdAt": NOW.isoformat(),
                                    "author": {"login": "bot"},
                                    "reactions": {
                                        "totalCount": reaction_total,
                                        "nodes": [],
                                    },
                                }
                            ],
                        },
                        "reviews": {"totalCount": review_total, "nodes": []},
                    }
                }
            }
        }

    def test_complete_graphql_evidence_is_returned_with_viewer(self):
        gh, runner = make_github()
        runner.when(["api", "graphql"], self.response())
        result = gh.reviewer_evidence(9)
        self.assertEqual(result["viewer"], "bot")
        self.assertEqual(len(result["comments"]), 1)
        self.assertEqual(result["reviews"], [])

    def test_partial_comments_reviews_or_reactions_fail_closed(self):
        for response in (
            self.response(reaction_total=1),
            self.response(review_total=1),
            {"data": {"repository": {"pullRequest": None}}},
        ):
            with self.subTest(response=response):
                gh, runner = make_github()
                runner.when(["api", "graphql"], response)
                with self.assertRaisesRegex(ForemanError, "failing closed"):
                    gh.reviewer_evidence(9)
