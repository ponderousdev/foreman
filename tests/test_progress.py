"""Display-only dispatch narration (#83): the liveness formatter, the
heartbeat-preserving wait loop (its kill/timeout contract is load-bearing),
and the TTY-vs-non-TTY reporter policy. All deterministic — injected
stream/isatty/clock, no sleeping, no real TTY."""

from __future__ import annotations

import io
import unittest

from foreman.progress import (
    HEARTBEAT_S,
    DispatchReporter,
    liveness_text,
    wait_with_heartbeat,
)
from foreman.runner import ExitStatus, WaitTimeout


class LivenessText(unittest.TestCase):
    def test_formats_elapsed_and_timeout(self):
        self.assertEqual(liveness_text(0, 5400), "still running 0s / 90m")
        self.assertEqual(liveness_text(150, 5400), "still running 2m30s / 90m")
        self.assertEqual(liveness_text(45, 120), "still running 45s / 2m")
        self.assertEqual(liveness_text(120, 120), "still running 2m / 2m")

    def test_elapsed_is_clamped_to_timeout(self):
        # A slice landing just past the deadline must never read over 100%.
        self.assertEqual(liveness_text(6000, 5400), "still running 90m / 90m")


class _FakeClock:
    """A side-effect-free clock read; `wait_fn` is what advances it, exactly as
    a real wait consumes wall time."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _driven_wait(clock: _FakeClock, *, timeouts: int, result: ExitStatus):
    """A wait_fn that consumes `slice_s` of the fake clock each call, raising
    WaitTimeout `timeouts` times before returning `result`."""
    calls = {"n": 0}

    def wait_fn(slice_s: int) -> ExitStatus:
        clock.t += slice_s
        calls["n"] += 1
        if calls["n"] <= timeouts:
            raise WaitTimeout("still running")
        return result

    return wait_fn, calls


class WaitWithHeartbeat(unittest.TestCase):
    def test_returns_exit_status_and_ticks_until_exit(self):
        clock = _FakeClock()
        done = ExitStatus(code=0)
        wait_fn, calls = _driven_wait(clock, timeouts=2, result=done)
        ticks: list[float] = []
        status = wait_with_heartbeat(wait_fn, 100, ticks.append, slice_s=1, now=clock)
        self.assertIs(status, done)  # identity: the wait's own result, verbatim
        self.assertEqual(calls["n"], 3)  # 2 timeouts + the returning call
        self.assertEqual(ticks, [1.0, 2.0])  # one per timed-out slice, increasing

    def test_never_ticks_when_already_exited(self):
        clock = _FakeClock()
        wait_fn, _ = _driven_wait(clock, timeouts=0, result=ExitStatus(code=0))
        ticks: list[float] = []
        wait_with_heartbeat(wait_fn, 100, ticks.append, slice_s=1, now=clock)
        self.assertEqual(ticks, [])

    def test_raises_wait_timeout_at_deadline_never_past_it(self):
        clock = _FakeClock()
        # Always alive: the loop must raise at the deadline (so the caller's
        # kill path fires) and never tick on or past it.
        wait_fn, _ = _driven_wait(clock, timeouts=10_000, result=ExitStatus(code=0))
        ticks: list[float] = []
        with self.assertRaises(WaitTimeout):
            wait_with_heartbeat(wait_fn, 5, ticks.append, slice_s=1, now=clock)
        self.assertEqual(ticks, [1.0, 2.0, 3.0, 4.0])  # never 5.0 (the deadline)


class ReporterAcknowledgment(unittest.TestCase):
    def test_start_names_branch_and_log_path(self):
        # AC1: the immediate per-unit acknowledgment carries branch + log path.
        buf = io.StringIO()
        DispatchReporter(stream=buf, isatty=False).unit(7).start(
            "foreman/feat/7-x", ".foreman/units/7/agent.log"
        )
        out = buf.getvalue()
        self.assertIn("#7", out)
        self.assertIn("foreman/feat/7-x", out)
        self.assertIn(".foreman/units/7/agent.log", out)
        self.assertTrue(out.endswith("\n"))
        self.assertEqual(out.count("\n"), 1)  # one clean line


class ReporterLiveness(unittest.TestCase):
    def test_non_tty_emits_throttled_plain_lines(self):
        # AC3 non-TTY: plain grep-friendly #N: lines, throttled on elapsed, no \r.
        buf = io.StringIO()
        reporter = DispatchReporter(stream=buf, isatty=False, workers=1)
        self.assertFalse(reporter.spinner_ok)  # a pipe/CI/log is never a spinner
        unit = reporter.unit(5)
        unit.heartbeat(1, 5400)  # first beat → emits
        unit.heartbeat(2, 5400)  # within HEARTBEAT_S of the first → suppressed
        unit.heartbeat(1 + HEARTBEAT_S + 1, 5400)  # past the window → emits
        lines = [ln for ln in buf.getvalue().splitlines() if ln]
        self.assertEqual(len(lines), 2)
        self.assertTrue(
            all(ln.startswith("foreman: #5: still running") for ln in lines)
        )
        self.assertNotIn("\r", buf.getvalue())

    def test_tty_single_unit_uses_in_place_spinner(self):
        # AC3 TTY: an interactive single-unit dispatch updates one line in place.
        buf = io.StringIO()
        reporter = DispatchReporter(stream=buf, isatty=True, workers=1)
        self.assertTrue(reporter.spinner_ok)
        unit = reporter.unit(5)
        unit.heartbeat(1, 5400)
        unit.heartbeat(2, 5400)
        out = buf.getvalue()
        self.assertIn("\r", out)
        self.assertEqual(out.count("\n"), 0)  # no newline spam until settle()
        self.assertEqual(out.count("\r"), 2)  # one in-place update per beat
        unit.settle()
        self.assertTrue(buf.getvalue().endswith("\n"))

    def test_multi_unit_tty_falls_back_to_plain_prefixed_lines(self):
        # Constraint: max_parallel > 1 uses per-unit prefixes, never one spinner —
        # even on a TTY (concurrent \r frames would interleave into garbage).
        buf = io.StringIO()
        reporter = DispatchReporter(stream=buf, isatty=True, workers=3)
        self.assertFalse(reporter.spinner_ok)
        reporter.unit(5).heartbeat(1, 5400)
        out = buf.getvalue()
        self.assertNotIn("\r", out)
        self.assertTrue(out.startswith("foreman: #5: "))


class ReporterLabel(unittest.TestCase):
    def test_label_overrides_the_unit_number_prefix(self):
        # #125: vet and shepherd narrate through the same UnitProgress but are
        # not dispatch units — a label replaces the #N prefix on every line
        # kind (phase, heartbeat, terminal), and #0 never leaks.
        buf = io.StringIO()
        unit = DispatchReporter(stream=buf, isatty=False).unit(0, label="vet")
        unit.phase("analyzing milestone #7")
        unit.heartbeat(1, 5400)
        unit.terminal("analysis complete")
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(ln.startswith("foreman: vet: ") for ln in lines))
        self.assertNotIn("#0", buf.getvalue())

    def test_default_prefix_is_unchanged(self):
        buf = io.StringIO()
        DispatchReporter(stream=buf, isatty=False).unit(7).phase("verifying")
        self.assertEqual(buf.getvalue(), "foreman: #7: verifying\n")


class ReporterHonesty(unittest.TestCase):
    def test_phase_and_terminal_are_prefixed_lines_only(self):
        # AC4-adjacent: narration is its own #N: channel — it never renders a
        # summary table (the summary stays report.summary_table's job, unchanged).
        buf = io.StringIO()
        unit = DispatchReporter(stream=buf, isatty=False).unit(9)
        unit.phase("verifying: task verify")
        unit.terminal("pr-open", "https://example.test/pr/1")
        lines = buf.getvalue().splitlines()
        self.assertEqual(
            lines,
            [
                "foreman: #9: verifying: task verify",
                "foreman: #9: pr-open — https://example.test/pr/1",
            ],
        )


if __name__ == "__main__":
    unittest.main()
