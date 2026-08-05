"""Watch-mode plumbing that must not regress: interval parsing and the
per-tick heartbeat line (single grep-friendly line, PR-state breakdown)."""

from __future__ import annotations

import re
import unittest

from foreman.shepherd import PrWork
from foreman.util import ForemanError
from foreman.watch import format_tick, format_tick_begin, parse_interval


class ParseInterval(unittest.TestCase):
    def test_units(self):
        self.assertEqual(parse_interval("300"), 300)
        self.assertEqual(parse_interval("300s"), 300)
        self.assertEqual(parse_interval("5m"), 300)
        self.assertEqual(parse_interval("1h"), 3600)

    def test_rejects_garbage(self):
        for bad in ("", "5 minutes", "m5", "-5m"):
            with self.assertRaises(ForemanError):
                parse_interval(bad)

    def test_rejects_non_positive(self):
        # 0 would turn the watch loop into a tight spin.
        for bad in ("0", "0s", "0m", "0h"):
            with self.assertRaises(ForemanError):
                parse_interval(bad)


def _pr(unit: int, state: str) -> PrWork:
    return PrWork(
        number=unit,
        unit_number=unit,
        branch=f"foreman/{unit}",
        url=f"https://example.test/pr/{unit}",
        title=f"unit {unit}",
        state=state,
    )


class FormatTick(unittest.TestCase):
    def _line(self, worked: list[PrWork]) -> str:
        return format_tick(
            open_units=1,
            dispatched=0,
            waiting=0,
            failed=0,
            worked=worked,
            total_cost=3.76,
        )

    def test_includes_open_and_state_breakdown(self):
        # AC: the line carries total open foreman PRs and a per-state
        # breakdown (at minimum ready and escalated).
        line = self._line([_pr(1, "ready"), _pr(2, "escalated"), _pr(3, "fixed")])
        self.assertIn("prs-open=3", line)
        self.assertIn("prs-ready=1", line)
        self.assertIn("prs-escalated=1", line)

    def test_escalation_is_visible(self):
        # AC: a tick whose shepherd pass escalates a PR shows a nonzero
        # escalated count — the #89 breakage this issue is about.
        line = self._line([_pr(1, "ready"), _pr(2, "escalated"), _pr(3, "escalated")])
        match = re.search(r"prs-escalated=(\d+)", line)
        self.assertIsNotNone(match)
        self.assertEqual(int(match.group(1)), 2)

    def test_no_open_prs_is_all_zero(self):
        # A quiet tick keeps the same stable columns, all zero.
        line = self._line([])
        self.assertIn("prs-open=0", line)
        self.assertIn("prs-ready=0", line)
        self.assertIn("prs-escalated=0", line)

    def test_single_line_and_grep_stable(self):
        # AC: the line stays single-line and stable for grep. Each token is a
        # unique key=value (no bare `prs:` label, no duplicated key) so a
        # per-key grep is unambiguous.
        line = self._line([_pr(1, "ready")])
        self.assertNotIn("\n", line)
        self.assertRegex(
            line,
            r"^open=\d+ dispatched=\d+ waiting=\d+ failed=\d+ "
            r"prs-open=\d+ prs-ready=\d+ prs-escalated=\d+ cost=\$\d+\.\d{2}$",
        )


class FormatTickBegin(unittest.TestCase):
    def test_leads_with_open_and_trails_the_free_text_label(self):
        # #83 comment: emitted at tick START so a long (agent-running) tick is
        # not mistaken for a stall. Numeric open= leads; the space-bearing label
        # trails, so the line stays grep-parseable.
        line = format_tick_begin(target_label="milestone 'M4' (#5)", open_units=4)
        self.assertEqual(line, "begin open=4 target=milestone 'M4' (#5)")

    def test_single_line_and_open_is_greppable(self):
        line = format_tick_begin(target_label="issue #83", open_units=1)
        self.assertNotIn("\n", line)
        self.assertRegex(line, r"^begin open=\d+ target=")


if __name__ == "__main__":
    unittest.main()
