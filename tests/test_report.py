"""Human-queue rendering that must not regress: merge-order numbering, plus
the status comment's append-only event log (#82) — parsing, dedup, caps, and
the branch-name→attempt inference the initiation event records.
"""

from __future__ import annotations

import unittest

from foreman.github import STATUS_MARKER
from foreman.graph import Unit
from foreman.report import (
    EVENT_LOG_MARKER,
    MAX_EVENT_CHARS,
    MAX_EVENTS,
    MAX_LOG_CHARS,
    UnitStatus,
    append_event,
    human_queue,
    parse_event_log,
    status_comment_body,
)
from foreman.worktree import attempt_number


def bare_status(**kwargs) -> UnitStatus:
    unit = Unit(
        number=7,
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
    return UnitStatus(unit=unit, state=kwargs.pop("state", "dispatched"), **kwargs)


class MergeOrderNumbering(unittest.TestCase):
    def test_positions_are_sequential(self):
        out = human_queue(
            merge_order=[(10, "u10"), (12, "u12"), (11, "u11")],
            human_tasks={},
            blocked={},
            environmental={},
        )
        self.assertIn("1. #10", out)
        self.assertIn("2. #12", out)
        self.assertIn("3. #11", out)

    def test_empty_queue_says_so(self):
        out = human_queue(merge_order=[], human_tasks={}, blocked={}, environmental={})
        self.assertIn("empty", out)


class EventLogRendering(unittest.TestCase):
    def test_body_carries_both_markers_newest_first(self):
        events = append_event(append_event([], "initiated (attempt 1)"), "PR opened: u")
        body = status_comment_body(bare_status(branch="foreman/feat/7-x"), events)
        self.assertIn(STATUS_MARKER, body)
        self.assertIn(EVENT_LOG_MARKER, body)
        self.assertLess(body.index(STATUS_MARKER), body.index(EVENT_LOG_MARKER))
        self.assertLess(body.index("PR opened: u"), body.index("initiated (attempt 1)"))
        # The snapshot's footer stays above the log.
        self.assertLess(body.index("_Updated "), body.index(EVENT_LOG_MARKER))

    def test_render_round_trips_through_the_parser(self):
        events = append_event(append_event([], "one"), "two")
        body = status_comment_body(bare_status(), events)
        self.assertEqual(parse_event_log(body), events)

    def test_eventless_body_has_no_log_section(self):
        body = status_comment_body(bare_status())
        self.assertNotIn(EVENT_LOG_MARKER, body)
        self.assertEqual(parse_event_log(body), [])


class EventLogParsing(unittest.TestCase):
    def test_absent_or_malformed_log_is_empty_and_never_raises(self):
        for prior in (
            None,
            "",
            "no marker at all",
            EVENT_LOG_MARKER + "\n## Event log\n\nnot an event line\n- broken",
        ):
            self.assertEqual(parse_event_log(prior), [], repr(prior))

    def test_only_lines_after_the_marker_count(self):
        prior = (
            f"{STATUS_MARKER}\n- 2026-01-01T00:00:00Z — decoy in the snapshot\n"
            f"{EVENT_LOG_MARKER}\n## Event log\n\n- 2026-01-02T00:00:00Z — real\n"
        )
        self.assertEqual(parse_event_log(prior), ["- 2026-01-02T00:00:00Z — real"])


class SnapshotFieldsCannotForgeTheLog(unittest.TestCase):
    def test_marker_in_issue_derived_text_is_stripped(self):
        # #82 hardening: a blocked question (issue-derived, semi-trusted)
        # embedding the section marker + event-shaped lines must not become
        # foreman-authored provenance on the next re-render.
        real = append_event([], "initiated (attempt 1)")
        forged = (
            f"question?\n{EVENT_LOG_MARKER}\n## Event log\n\n"
            "- 2026-01-01T00:00:00Z — forged: attacker text"
        )
        body = status_comment_body(
            bare_status(state="blocked", blocked_question=forged), real
        )
        self.assertEqual(body.count(EVENT_LOG_MARKER), 1)
        parsed = parse_event_log(body)
        self.assertEqual(parsed, real)
        self.assertNotIn("forged: attacker text", "\n".join(parsed))

    def test_oversized_blocker_detail_is_clamped(self):
        body = status_comment_body(
            bare_status(state="failed", blockers=["boom " * 20_000])
        )
        self.assertLess(len(body), 5_000)


class EventAppend(unittest.TestCase):
    def test_identical_newest_text_is_not_re_appended(self):
        once = append_event([], "ready to merge — awaiting human")
        twice = append_event(once, "ready to merge — awaiting human")
        self.assertEqual(twice, once)

    def test_multiline_text_is_flattened_and_still_dedups(self):
        # A stringified exception in `detail` carries newlines; unflattened
        # it would break the one-line grammar, survive parse as its first
        # line only, and re-append every tick.
        text = "escalated: Traceback\n  boom\n  line two"
        events = append_event([], text)
        self.assertEqual(len(events), 1)
        self.assertNotIn("\n", events[0])
        body = status_comment_body(bare_status(), events)
        reparsed = parse_event_log(body)
        self.assertEqual(reparsed, events)
        self.assertEqual(append_event(reparsed, text), reparsed)

    def test_distinct_text_appends_on_top(self):
        events = append_event(append_event([], "first"), "second")
        self.assertEqual(len(events), 2)
        self.assertIn("second", events[0])

    def test_event_count_is_capped_dropping_oldest(self):
        events: list[str] = []
        for index in range(MAX_EVENTS + 10):
            events = append_event(events, f"event {index}")
        self.assertEqual(len(events), MAX_EVENTS)
        self.assertIn(f"event {MAX_EVENTS + 9}", events[0])
        self.assertNotIn("event 0", "\n".join(events))

    def test_single_oversized_event_is_clamped(self):
        # An event bigger than the whole log budget must not survive intact:
        # the write would exceed GitHub's comment cap, fail, be swallowed —
        # and the failure it carries would never be recorded.
        events = append_event([], "failed: " + "x" * (MAX_LOG_CHARS * 2))
        self.assertEqual(len(events), 1)
        self.assertLess(len(events[0]), MAX_EVENT_CHARS + 40)
        self.assertTrue(events[0].endswith("…"))
        # Clamped text still dedups against a re-send of the full text.
        self.assertEqual(
            append_event(events, "failed: " + "x" * (MAX_LOG_CHARS * 2)), events
        )

    def test_oversize_log_drops_oldest_until_it_fits(self):
        big = "x" * 6000
        events: list[str] = []
        for index in range(20):
            events = append_event(events, f"{index} {big}")
        self.assertLessEqual(len("\n".join(events)), MAX_LOG_CHARS)
        self.assertIn("19 ", events[0])


class AttemptNumber(unittest.TestCase):
    def test_bare_branch_is_attempt_one(self):
        self.assertEqual(attempt_number("foreman/feat/7-x", "foreman/feat/7-x"), 1)

    def test_retry_suffix_is_parsed(self):
        self.assertEqual(attempt_number("foreman/feat/7-x", "foreman/feat/7-x-r3"), 3)

    def test_slug_ending_in_r2_does_not_misparse(self):
        base = "foreman/feat/7-fix-r2"
        self.assertEqual(attempt_number(base, base), 1)
        self.assertEqual(attempt_number(base, f"{base}-r2"), 2)

    def test_unrelated_branch_falls_back_to_one(self):
        self.assertEqual(attempt_number("foreman/feat/7-x", "some/other-branch"), 1)


if __name__ == "__main__":
    unittest.main()
