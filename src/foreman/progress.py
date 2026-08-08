"""Display-only dispatch narration: per-unit acknowledgment, phase transitions,
and a liveness indicator during the agent run (#83). A peer to report.py —
output only, never read back for decisions, and it changes no control flow.

The runner seam blocks in ``runner.wait()`` for the whole agent run, so a
dispatch looks wedged when it is merely working. ``wait_with_heartbeat`` slices
that single wait into short steps so a liveness callback fires periodically,
*without* altering the wait's contract: it still returns the exit status the
instant the unit exits and still raises ``WaitTimeout`` at the total deadline —
so the caller's kill-on-timeout path is unchanged.

TTY vs non-TTY: an interactive single-unit dispatch shows an in-place spinner;
everything else (multiple concurrent units, or a pipe / CI / watch log) emits
plain, grep-friendly ``foreman: #N: ...`` lines. Multi-unit never shares one
spinner — concurrent carriage returns would interleave into garbage — so each
unit narrates under its own ``#N:`` prefix instead.
"""

from __future__ import annotations

import itertools
import sys
import threading
import time
from collections.abc import Callable
from typing import TextIO

from foreman.runner import ExitStatus, WaitTimeout

# Non-TTY liveness cadence: at most one plain line this often, keyed on the
# elapsed value passed in (never a wall clock) so it is deterministically
# testable. Frequent enough to tell working from wedged, sparse enough that a
# multi-hour watch run stays grep-friendly.
HEARTBEAT_S = 30.0
_SPINNER_FRAMES = "|/-\\"


def _fmt_duration(seconds: float) -> str:
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    if minutes and secs:
        return f"{minutes}m{secs:02d}s"
    if minutes:
        return f"{minutes}m"
    return f"{secs}s"


def liveness_text(elapsed_s: float, timeout_s: float) -> str:
    """Pure one-liner for the liveness indicator: elapsed against the unit's
    timeout, e.g. ``still running 2m30s / 90m``. Elapsed is clamped to the
    timeout so a slice that lands just past the deadline never reads over
    100%."""
    elapsed = max(elapsed_s, 0.0)
    if timeout_s > 0:
        elapsed = min(elapsed, timeout_s)
    return f"still running {_fmt_duration(elapsed)} / {_fmt_duration(timeout_s)}"


def wait_with_heartbeat(
    wait_fn: Callable[[int], ExitStatus],
    timeout_s: int,
    on_tick: Callable[[float], None],
    *,
    slice_s: int = 1,
    now: Callable[[], float] = time.monotonic,
) -> ExitStatus:
    """Drive ``wait_fn`` in ``slice_s`` steps, calling ``on_tick(elapsed)``
    between steps, while preserving the wait contract exactly.

    Returns the ``ExitStatus`` the moment ``wait_fn`` reports an exit; raises
    ``WaitTimeout`` once the absolute deadline passes (kill timing therefore
    shifts by at most one slice — negligible against a minutes-long timeout).
    Anchored to a single ``deadline`` so per-slice latency cannot accumulate
    over a long run, and ``on_tick`` fires only while time remains, so a slice
    landing on the deadline goes straight to the raise instead of emitting a
    cosmetic ``elapsed == timeout`` beat. ``now`` must be a side-effect-free
    clock read; it is called several times per iteration.
    """
    start = now()
    deadline = start + timeout_s
    while now() < deadline:
        try:
            return wait_fn(slice_s)
        except WaitTimeout:
            if now() < deadline:
                on_tick(now() - start)
    raise WaitTimeout(f"still running after {timeout_s}s")


class DispatchReporter:
    """Owns the output stream, the TTY decision, the clock, and the one lock
    that serializes every write so concurrent dispatch workers can never
    interleave a line. Hand out one ``UnitProgress`` per unit via ``unit()``.
    """

    def __init__(
        self,
        *,
        workers: int = 1,
        stream: TextIO | None = None,
        isatty: bool | None = None,
        now: Callable[[], float] = time.monotonic,
    ):
        self._stream = stream
        self.now = now
        self._lock = threading.Lock()
        # A single worker owning a real TTY is the only case a carriage-return
        # spinner is safe: with >1 worker the units' \r frames would interleave,
        # so they fall back to plain per-#N: lines (spec #83).
        self.spinner_ok = workers == 1 and self._resolve_isatty(isatty)

    def _resolve_isatty(self, isatty: bool | None) -> bool:
        if isatty is not None:
            return isatty
        stream = self._stream if self._stream is not None else sys.stdout
        try:
            return bool(stream.isatty())
        except (AttributeError, ValueError):
            return False

    def _emit(self, text: str) -> None:
        # Resolve the stream at write time so a redirected sys.stdout (tests,
        # captured output) is honored. A closed pipe or detached TTY must never
        # break a dispatch or, worse, skip a kill — swallow write errors.
        stream = self._stream if self._stream is not None else sys.stdout
        with self._lock:
            try:
                stream.write(text)
                stream.flush()
            except (OSError, ValueError):
                pass

    def unit(self, number: int, label: str | None = None) -> UnitProgress:
        return UnitProgress(self, number, label=label)


class UnitProgress:
    """One unit's narration. Phase/terminal lines are plain and grep-friendly;
    heartbeats are an in-place spinner on an interactive single-unit dispatch
    and throttled plain lines otherwise."""

    def __init__(
        self, reporter: DispatchReporter, number: int, label: str | None = None
    ):
        self._reporter = reporter
        # Display prefix only. Dispatch narrates as ``#N``; vet and shepherd
        # (#125) pass a label (``vet``, ``#N PR #M``) because their runs are
        # not dispatch units and ``#0`` would read as one.
        self._prefix = label if label is not None else f"#{number}"
        self.now = reporter.now
        self._frames = itertools.cycle(_SPINNER_FRAMES)
        self._spinner_open = False  # a \r line is pending its closing newline
        self._last_beat: float | None = None  # elapsed of the last plain beat

    def _line(self, text: str) -> None:
        # Close any pending spinner line first so a plain line never glues onto
        # a carriage-returned one.
        self.settle()
        self._reporter._emit(f"foreman: {self._prefix}: {text}\n")

    def start(self, branch: str, log_path: object) -> None:
        """Immediate per-unit acknowledgment (AC1): branch + live log path."""
        self._line(f"dispatching branch={branch} log={log_path}")

    def phase(self, text: str) -> None:
        self._line(text)

    def heartbeat(self, elapsed_s: float, timeout_s: float) -> None:
        if self._reporter.spinner_ok:
            frame = next(self._frames)
            self._reporter._emit(
                f"\rforeman: {self._prefix}: {frame} "
                f"{liveness_text(elapsed_s, timeout_s)}"
            )
            self._spinner_open = True
            return
        if self._last_beat is not None and elapsed_s - self._last_beat < HEARTBEAT_S:
            return
        self._last_beat = elapsed_s
        self._line(liveness_text(elapsed_s, timeout_s))

    def settle(self) -> None:
        """Newline-close an open spinner line. Idempotent — safe to call after
        the wait, again at the top of _conclude, and on the reattach path."""
        if self._spinner_open:
            self._reporter._emit("\n")
            self._spinner_open = False

    def terminal(self, status: str, note: str = "") -> None:
        """One honest closing line for a terminal outcome (pr-open, failed,
        blocked, waiting, stale, refused). Narrates exactly what happened."""
        self._line(f"{status} — {note}" if note else status)
