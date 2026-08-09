"""MockRunner — proves the protocol *shape* and drives dispatch in tests.

What it proves: dispatch compiles against the Runner protocol and drives
spawn → wait → (verify) → preserve/cleanup without knowing which runner is
behind the seam. What it must never be credited with: process groups, exit
statuses, PID reuse, isolation — those belong to the LocalRunner subprocess
tests and the environment tiers (docs/architecture/tests.md).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from foreman.runner import ExecResult, ExitStatus, Handle, UnitSpec, WaitTimeout


class MockRunner:
    """In-memory Runner.

    `agent` runs at spawn time to simulate the unit's work (write a result
    contract, commit to the worktree, ...) and returns the exit code wait()
    will report. `caps` is what capabilities() advertises. `running` marks
    handles wait() should treat as still-alive (raising WaitTimeout).
    """

    def __init__(
        self,
        *,
        agent: Callable[[UnitSpec], int] | None = None,
        caps: set[str] | None = None,
    ):
        self.agent = agent
        self.caps = set(caps or ())
        self.spawned: list[UnitSpec] = []
        self.waited: list[int] = []
        self.killed: list[int] = []
        self.preserved: list[int] = []
        self.cleaned: list[int] = []
        self.execs: list[tuple[int, list[str]]] = []
        self.running: set[int] = set()
        self._codes: dict[int, int] = {}

    def spawn(self, spec: UnitSpec) -> Handle:
        self.spawned.append(spec)
        code = self.agent(spec) if self.agent else 0
        self._codes[spec.unit] = code
        return Handle(
            runner="mock",
            unit=spec.unit,
            run_dir=str(spec.run_dir),
            payload={"mock": True},
        )

    def wait(self, handle: Handle, timeout_s: int) -> ExitStatus:
        self.waited.append(handle.unit)
        if handle.unit in self.running:
            raise WaitTimeout(f"unit #{handle.unit} still running (mock)")
        if handle.unit not in self._codes:
            return ExitStatus(code=None, abnormal=True, detail="no recorded status")
        return ExitStatus(code=self._codes[handle.unit])

    def kill(self, handle: Handle) -> None:
        self.killed.append(handle.unit)
        self.running.discard(handle.unit)
        self._codes[handle.unit] = 128 + 15

    def logs(self, handle: Handle) -> Iterator[str]:
        yield from ()

    def exec(self, handle: Handle, cmd: list[str]) -> ExecResult:
        self.execs.append((handle.unit, list(cmd)))
        return ExecResult(returncode=0, stdout="", stderr="")

    def reference(self, handle: Handle) -> str:
        return f"mock-unit={handle.unit}"

    def preserve(self, handle: Handle) -> None:
        self.preserved.append(handle.unit)

    def cleanup(self, handle: Handle) -> None:
        self.cleaned.append(handle.unit)

    def capabilities(self) -> set[str]:
        return set(self.caps)
