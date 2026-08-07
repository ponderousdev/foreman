"""#82: the status comment is a snapshot plus an append-only event log, still
one comment per unit edited in place. End-to-end over the fake gh transport:
create vs edit, newest-first ordering, log preservation across an event-less
refresh, dedup, and shepherd's snapshot-preserving append.
"""

from __future__ import annotations

import unittest

from foreman import report
from foreman.github import STATUS_MARKER
from foreman.graph import Unit
from foreman.report import EVENT_LOG_MARKER, UnitStatus
from tests.fakes import FakeRunner, make_github

UNIT = 7


def unit() -> Unit:
    return Unit(
        number=UNIT,
        title="U7",
        state="OPEN",
        state_reason=None,
        body="",
        url="",
        labels=[],
        issue_type=None,
        milestone=None,
        parent=None,
    )


def status(state: str = "dispatched", **kwargs) -> UnitStatus:
    return UnitStatus(unit=unit(), state=state, **kwargs)


def stub_comments(runner: FakeRunner, comments: list[dict]) -> None:
    runner.when(
        ["api", f"repos/owner/repo/issues/{UNIT}/comments", "--paginate", "--slurp"],
        [comments],
    )


def own_comment(body: str, *, comment_id: int = 2) -> dict:
    return {"id": comment_id, "body": body, "user": {"login": "bot"}}


def written(runner: FakeRunner, method: str) -> list[str]:
    """Bodies sent to gh via `-F body=@-` for the given REST method."""
    return [
        text
        for argv, text in runner.calls
        if argv[:3] == ["api", "--method", method] and text is not None
    ]


class FirstWriteCreatesTheComment(unittest.TestCase):
    def test_no_prior_comment_posts_marker_and_event(self):
        gh, runner = make_github()
        stub_comments(runner, [])
        runner.when(["api", "--method", "POST"], "{}")
        report.update_status_comment(
            gh, status(branch="foreman/feat/7-x"), event="initiated (attempt 1)"
        )
        posts = written(runner, "POST")
        self.assertEqual(len(posts), 1)
        self.assertIn(STATUS_MARKER, posts[0])
        self.assertIn(EVENT_LOG_MARKER, posts[0])
        self.assertIn("initiated (attempt 1)", posts[0])
        self.assertEqual(runner.called_with_prefix(["api", "--method", "PATCH"]), [])
        # Known-absent goes straight to POST: the first read already proved
        # there is no comment, so no second lookup happens on the create path.
        reads = runner.called_with_prefix(
            ["api", f"repos/owner/repo/issues/{UNIT}/comments"]
        )
        self.assertEqual(len(reads), 1)


class SubsequentWritesEditInPlace(unittest.TestCase):
    def _prior(self, *events: str) -> str:
        return report.status_comment_body(
            status(),
            [f"- 2026-08-0{n + 1}T00:00:00Z — {e}" for n, e in enumerate(events)],
        )

    def test_new_event_lands_on_top_and_old_survives(self):
        gh, runner = make_github()
        stub_comments(runner, [own_comment(self._prior("initiated (attempt 1)"))])
        runner.when(
            ["api", "--method", "PATCH", "repos/owner/repo/issues/comments/2"], "{}"
        )
        report.update_status_comment(
            gh, status("pr-open", pr_url="u"), event="PR opened: u"
        )
        patches = written(runner, "PATCH")
        self.assertEqual(len(patches), 1)
        body = patches[0]
        self.assertIn("initiated (attempt 1)", body)
        self.assertLess(body.index("PR opened: u"), body.index("initiated (attempt 1)"))
        # Still exactly one comment: no POST alongside the edit.
        self.assertEqual(runner.called_with_prefix(["api", "--method", "POST"]), [])

    def test_eventless_refresh_preserves_the_log(self):
        # Regression: the dispatch conclusion rewrites the snapshot with no
        # event of its own (e.g. `waiting`) and must not wipe the initiation.
        gh, runner = make_github()
        stub_comments(runner, [own_comment(self._prior("initiated (attempt 1)"))])
        runner.when(
            ["api", "--method", "PATCH", "repos/owner/repo/issues/comments/2"], "{}"
        )
        report.update_status_comment(gh, status("waiting"))
        body = written(runner, "PATCH")[0]
        self.assertIn(EVENT_LOG_MARKER, body)
        self.assertIn("initiated (attempt 1)", body)

    def test_same_event_twice_skips_the_write_entirely(self):
        # Dedup is a true no-op: a PR sitting escalated/ready across many
        # watch ticks must not PATCH identical content every tick.
        gh, runner = make_github()
        stub_comments(runner, [own_comment(self._prior("escalated: boom"))])
        report.append_status_event(gh, UNIT, "escalated: boom")
        self.assertEqual(runner.called_with_prefix(["api", "--method"]), [])

    def test_each_write_reads_comments_exactly_once(self):
        # The found comment id is threaded into the upsert: a duplicate
        # lookup could flake after the first read succeeded and silently
        # drop the event.
        gh, runner = make_github()
        stub_comments(runner, [own_comment(self._prior("initiated (attempt 1)"))])
        runner.when(
            ["api", "--method", "PATCH", "repos/owner/repo/issues/comments/2"], "{}"
        )
        report.update_status_comment(gh, status("pr-open"), event="PR opened: u")
        reads = runner.called_with_prefix(
            ["api", f"repos/owner/repo/issues/{UNIT}/comments"]
        )
        self.assertEqual(len(reads), 1)


class ShepherdAppendPreservesSnapshot(unittest.TestCase):
    def test_snapshot_section_is_carried_over_verbatim(self):
        gh, runner = make_github()
        prior = report.status_comment_body(
            status("pr-open", branch="foreman/feat/7-x", pr_url="u"),
            ["- 2026-08-01T00:00:00Z — initiated (attempt 1)"],
        )
        stub_comments(runner, [own_comment(prior)])
        runner.when(
            ["api", "--method", "PATCH", "repos/owner/repo/issues/comments/2"], "{}"
        )
        report.append_status_event(gh, UNIT, "ready for review — human turn")
        body = written(runner, "PATCH")[0]
        expected = prior.split(EVENT_LOG_MARKER, 1)[0].rstrip("\n")
        self.assertEqual(body.split(EVENT_LOG_MARKER, 1)[0].rstrip("\n"), expected)
        self.assertIn("ready for review — human turn", body)
        self.assertIn("initiated (attempt 1)", body)

    def test_missing_comment_creates_a_log_only_one(self):
        gh, runner = make_github()
        stub_comments(runner, [])
        runner.when(["api", "--method", "POST"], "{}")
        report.append_status_event(gh, UNIT, "escalated: boom")
        body = written(runner, "POST")[0]
        self.assertTrue(body.startswith(STATUS_MARKER))
        self.assertIn("escalated: boom", body)


class DisplayOnlyNeverFailsARun(unittest.TestCase):
    def test_a_failing_read_is_swallowed(self):
        # No comments stub: the FakeRunner raises on the un-stubbed read.
        gh, runner = make_github()
        report.update_status_comment(gh, status(), event="initiated (attempt 1)")
        report.append_status_event(gh, UNIT, "escalated: boom")
        self.assertEqual(runner.called_with_prefix(["api", "--method"]), [])


if __name__ == "__main__":
    unittest.main()
