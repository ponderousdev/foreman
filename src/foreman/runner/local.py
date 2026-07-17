"""LocalRunner — subprocess execution in the unit worktree (v2.0).

The boundary is the bot devcontainer, not the process environment (D1/D2);
input is trusted-only (D4). `image` and `limits` on the spec are ignored —
documented, deliberate. The environment passed to the unit is exactly
`spec.env` (the caller-built allowlist, #13), never an os.environ merge.

Exit-code ground truth (#22/#23): the adapter is spawned through a wrapper
that records the shell wait status (128+N for signal deaths) to
`<run_dir>/exit-status` via temp-file + atomic rename. The wrapper is the
process-group leader, so v1's kill-the-group semantics are unchanged, and it
exits only after the rename — a dead unit with NO recorded status therefore
means the wrapper itself was killed: abnormal termination, reported as such,
never guessed. The wrapper installs a no-op TERM *handler* (`trap ':'`), not
an ignore: handlers reset to default across exec, so the adapter child still
dies on group-TERM while the wrapper survives long enough to record 143.
(An ignored signal would be inherited by the child and break kill().)

Liveness is PID + process start-time (/proc/<pid>/stat field 22) — a bare
PID is not a handle, because PIDs are reused. For processes this Foreman
spawned, the Popen object is used (which also reaps); for reattached
handles, /proc is probed and the recorded status file is read. Linux-only by
design: Foreman runs only in the bot devcontainer (D2).
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from foreman.config import Config
from foreman.runner import (
    ExecResult,
    ExitStatus,
    Handle,
    UnitSpec,
    WaitTimeout,
)
from foreman.util import ForemanError

STATUS_FILE = "exit-status"
STDOUT_LOG = "adapter-stdout.log"
KILL_GRACE_S = 10
_POLL_S = 0.1

# $0 is a label for ps; $1 is the status-file path; the rest is the adapter
# argv. See the module docstring for why the trap is a handler, not an ignore.
# The ready marker closes a race spawn() waits out: a group-TERM delivered
# before `trap` executes would kill the wrapper recordless, so "spawned" is
# defined as trap-installed — after that, kill() always yields a recorded
# 128+N, and dead-with-no-status keeps meaning something went genuinely
# wrong, not that the caller was quick.
_WRAPPER = """\
sf="$1"; shift
trap ':' TERM
: >"$sf.ready"
"$@"
ec=$?
printf '%s' "$ec" >"$sf.tmp" && mv -f "$sf.tmp" "$sf"
exit "$ec"
"""
# A live process that has not run `trap` yet is what we wait for; the marker
# normally appears within milliseconds. The cap only guards against a wedged
# interpreter, so it is generous — a shared-box scheduling stall must never
# make spawn() return before the trap is installed (that would let a prompt
# kill() misclassify a normal exit as abnormal).
_READY_WAIT_S = 30


def _proc_starttime(pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat (clock ticks since boot), or None if the
    process does not exist. Parsed after the last ')' — comm may contain
    spaces and parentheses."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None
    try:
        after_comm = text.rsplit(")", 1)[1].split()
        return after_comm[19]  # state is field 3 == index 0 after comm
    except IndexError:
        return None


def _default_docker_probe() -> bool:
    """Is a usable Docker daemon reachable? (D7: probed, never assumed.)"""
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


class LocalRunner:
    """See module docstring. One instance per Foreman process; safe to share
    across dispatch threads (spawn bookkeeping is per-PID)."""

    name = "local"

    def __init__(self, cfg: Config, *, docker_probe: Callable[[], bool] | None = None):
        self.cfg = cfg
        self._docker_probe = docker_probe or _default_docker_probe
        self._docker: bool | None = None
        self._procs: dict[int, subprocess.Popen] = {}

    # ── protocol ─────────────────────────────────────────────────────

    def spawn(self, spec: UnitSpec) -> Handle:
        spec.run_dir.mkdir(parents=True, exist_ok=True)
        status_path = spec.run_dir / STATUS_FILE
        status_path.unlink(missing_ok=True)  # a fresh spawn must never read stale truth
        stdout_path = spec.run_dir / STDOUT_LOG
        argv = [
            "/bin/sh",
            "-c",
            _WRAPPER,
            "foreman-unit-wrapper",
            str(status_path),
            *spec.cmd,
        ]
        with stdout_path.open("a", encoding="utf-8") as out_fh:
            proc = subprocess.Popen(
                argv,
                cwd=str(spec.workdir),
                env=spec.env,
                stdout=out_fh,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # wrapper becomes the group leader
            )
        self._procs[proc.pid] = proc
        ready = Path(f"{status_path}.ready")
        deadline = time.monotonic() + _READY_WAIT_S
        while not ready.exists() and proc.poll() is None:
            if time.monotonic() >= deadline:
                break  # wait() will classify whatever this is
            time.sleep(0.01)
        ready.unlink(missing_ok=True)
        starttime = _proc_starttime(proc.pid) or ""
        return Handle(
            runner=self.name,
            unit=spec.unit,
            run_dir=str(spec.run_dir),
            payload={
                "pid": proc.pid,
                "starttime": starttime,
                "workdir": str(spec.workdir),
                "status_file": str(status_path),
                "stdout_log": str(stdout_path),
            },
        )

    def wait(self, handle: Handle, timeout_s: int) -> ExitStatus:
        deadline = time.monotonic() + timeout_s
        while self._alive(handle):
            if time.monotonic() >= deadline:
                raise WaitTimeout(
                    f"unit #{handle.unit} still running after {timeout_s}s"
                )
            time.sleep(_POLL_S)
        return self._recorded_status(handle)

    def kill(self, handle: Handle) -> None:
        """TERM the group, grace, then KILL — v1 `_kill_group` semantics.
        Probes liveness first so a recycled PID is never signaled. Claims
        only what it does: DinD daemon-level descendants survive."""
        if not self._alive(handle):
            self._reap(handle)
            return
        pid = self._pid(handle)
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            self._reap(handle)
            return
        deadline = time.monotonic() + KILL_GRACE_S
        while time.monotonic() < deadline:
            if not self._alive(handle):
                self._reap(handle)
                return
            time.sleep(0.2)
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self._reap(handle)

    def logs(self, handle: Handle) -> Iterator[str]:
        for name in (STDOUT_LOG, "agent.log"):
            path = Path(handle.run_dir) / name
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                yield line

    def exec(self, handle: Handle, cmd: list[str]) -> ExecResult:
        """Supervisor-side command in the unit's workdir (git ops, not agent
        code) — runs with Foreman's own environment by design."""
        workdir = str(handle.payload.get("workdir") or handle.run_dir)
        try:
            proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise ForemanError(f"exec: command not found: {cmd[0]}") from exc
        return ExecResult(
            returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
        )

    def preserve(self, handle: Handle) -> None:
        """Local preservation is doing nothing destructive: the worktree and
        Claude session survive. A marker records the decision for triage."""
        marker = Path(handle.run_dir) / "preserved"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("preserved for triage; see resume-state.md\n", "utf-8")

    def cleanup(self, handle: Handle) -> None:
        """Drop process bookkeeping. Worktree removal is the commit handoff's
        job; logs under run_dir are never deleted here."""
        self._procs.pop(self._pid(handle), None)

    def capabilities(self) -> set[str]:
        """Computed per environment (D7): `docker` is probed. `ports` is
        derivable from max_parallel == 1 but deliberately withheld in v2.0
        (D9) — readiness races produce silent wrong work, and DinD children
        escape the group kill. `untrusted-input` is never advertised:
        co-location is the boundary local does not have (D4)."""
        if self._docker is None:
            self._docker = self._docker_probe()
        return {"docker"} if self._docker else set()

    # ── internals ────────────────────────────────────────────────────

    def _pid(self, handle: Handle) -> int:
        raw = handle.payload.get("pid")
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise ForemanError(f"handle for unit #{handle.unit} has no usable pid")
        try:
            return int(raw)
        except ValueError:
            raise ForemanError(
                f"handle for unit #{handle.unit} has no usable pid"
            ) from None

    def _alive(self, handle: Handle) -> bool:
        pid = self._pid(handle)
        proc = self._procs.get(pid)
        if proc is not None:
            return proc.poll() is None
        # Reattached handle: this process never spawned the pid. Probe
        # /proc and compare start-time so a recycled PID cannot fool us.
        current = _proc_starttime(pid)
        if current is None:
            return False
        recorded = str(handle.payload.get("starttime") or "")
        if not recorded or current != recorded:
            return False  # different process wearing a recycled PID
        return True

    def _reap(self, handle: Handle) -> None:
        proc = self._procs.get(self._pid(handle))
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def _recorded_status(self, handle: Handle) -> ExitStatus:
        self._reap(handle)
        status_path = Path(
            str(handle.payload.get("status_file") or "")
            or Path(handle.run_dir) / STATUS_FILE
        )
        if status_path.exists():
            raw = status_path.read_text(encoding="utf-8").strip()
            try:
                return ExitStatus(code=int(raw))
            except ValueError:
                return ExitStatus(
                    code=None,
                    abnormal=True,
                    detail=f"recorded status unreadable: {raw!r}",
                )
        return ExitStatus(
            code=None,
            abnormal=True,
            detail=(
                "process dead with no recorded exit status — the wrapper "
                "itself was killed (abnormal termination, not a normal exit)"
            ),
        )
