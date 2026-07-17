"""The Runner seam (spec: specs/foreman-v2.md, ADR 0003).

One protocol; `local` (v2.0), `sprite` (v2.1), and `docker` (v2.2) implement
it. Selection is config (`runner = "local"`); resolution lives in `create()`.
The seam must not leak: graph, GitHub, and eligibility code never branch on a
runner *name* — they consume advertised *capabilities* (`capabilities()`),
and tests/test_leak.py enforces it. Runner names describe WHERE execution
happens; concurrency, isolation, and capability are properties OF a runner,
never names FOR one.

Handles are a cache; GitHub and git are the truth (ADR 0002). A `Handle` is
opaque to everything except its owning runner, serialized under
`.foreman/runs/`, and must be liveness-checkable in a way PID reuse cannot
fool (LocalRunner: PID + process start-time). Exit status must survive a
Foreman restart: Linux hands an exit status only to the parent, so runners
record it out-of-band (LocalRunner: a status file written by the spawn
wrapper via atomic rename) and `wait()` supports a non-child mode — poll
liveness, then read the recorded status. A dead unit with no recorded status
is ABNORMAL, reported as such, never guessed.

v2.1 adds `start`/`fetch`/`put`/`attach`, driven by the Sprite handoff
(binary-safe transport for `git bundle` and the prompt). Deliberately not
built in v2.0 — do not add them speculatively.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from foreman.config import Config
from foreman.util import ForemanError, utc_now_iso

HANDLE_SCHEMA = 1


class WaitTimeout(ForemanError):
    """`wait()` gave up before the unit exited (the unit may still be alive).

    Raised — never returned — so a timeout cannot be mistaken for an exit
    status. The caller decides what a timeout means (v1 semantics: kill the
    process group, preserve the worktree, report timed-out).
    """


@dataclass(frozen=True)
class UnitSpec:
    """Everything a runner needs to start one unit of agent work.

    `cmd` is explicit because v1 spawns `[adapter, "run"]` vs
    `[adapter, "resume", ref]` — a spawn signature without cmd cannot express
    resume, and resume is load-bearing for triage.

    `env` is TOTAL: the runner passes exactly this environment, never a merge
    with its own (`os.environ.copy()` is the v1 bug the seam removes; #13).

    `image` and `limits` are ignored by LocalRunner (documented, deliberate);
    they exist so sprite/docker specs carry them without a protocol change.

    `gate_cmds` is the composed verify gate (baseline first, then
    capability-keyed additions in declaration order). It rides the spec as
    the defined transport for runners whose gate executes in the guest
    (sprite, v2.1 — #29/#30); under local, Foreman's own process runs the
    gate in the worktree and this field is informational.

    `unit` and `run_dir` identify the unit and its log/status directory —
    the runner mints the Handle from them and records the exit status under
    `run_dir` so a restarted Foreman can recover it.
    """

    unit: int
    workdir: Path
    run_dir: Path
    env: dict[str, str]
    cmd: tuple[str, ...]
    timeout_s: int
    image: str = ""
    limits: dict[str, object] = field(default_factory=dict)
    gate_cmds: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class ExitStatus:
    """How a unit ended. `code` is the recorded wait status (128+N for signal
    deaths). `abnormal` means dead-with-no-recorded-status: the wrapper itself
    was killed — never conflate it with a normal nonzero exit."""

    code: int | None
    abnormal: bool = False
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.abnormal


@dataclass(frozen=True)
class ExecResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass
class Handle:
    """Opaque, serializable reference to a spawned unit.

    `payload` belongs to the owning runner alone (LocalRunner stores
    pid + starttime + paths there); nothing else may interpret it.
    """

    runner: str
    unit: int
    run_dir: str
    payload: dict[str, object] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema": HANDLE_SCHEMA,
                "runner": self.runner,
                "unit": self.unit,
                "run_dir": self.run_dir,
                "payload": self.payload,
                "created_at": self.created_at,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> Handle:
        data = json.loads(text)
        if data.get("schema") != HANDLE_SCHEMA:
            raise ForemanError(
                f"handle: unsupported schema {data.get('schema')!r} "
                f"(expected {HANDLE_SCHEMA})"
            )
        return cls(
            runner=data["runner"],
            unit=data["unit"],
            run_dir=data["run_dir"],
            payload=data.get("payload") or {},
            created_at=data.get("created_at") or "",
        )


@runtime_checkable
class Runner(Protocol):
    """The seam. Only *where* an agent executes and *how commits return* may
    differ between implementations; everything else is shared supervisor code.

    `wait(handle, timeout_s)` blocks up to timeout_s then raises WaitTimeout;
    `timeout_s=0` is the non-blocking liveness poll (raises WaitTimeout if
    the unit is still running, else returns the recorded ExitStatus).
    """

    def spawn(self, spec: UnitSpec) -> Handle: ...

    def wait(self, handle: Handle, timeout_s: int) -> ExitStatus: ...

    def kill(self, handle: Handle) -> None: ...

    def logs(self, handle: Handle) -> Iterator[str]: ...

    def exec(self, handle: Handle, cmd: list[str]) -> ExecResult: ...

    def preserve(self, handle: Handle) -> None: ...

    def cleanup(self, handle: Handle) -> None: ...

    def capabilities(self) -> set[str]: ...


# ── handle store (.foreman/runs/) ────────────────────────────────────


def runs_dir(cfg: Config, root: Path) -> Path:
    return root / cfg.runtime_dir / "runs"


def handle_path(cfg: Config, root: Path, unit: int) -> Path:
    return runs_dir(cfg, root) / f"{unit}.json"


def save_handle(cfg: Config, root: Path, handle: Handle) -> Path:
    path = handle_path(cfg, root, handle.unit)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(handle.to_json() + "\n", encoding="utf-8")
    tmp.rename(path)  # atomic: a crash never leaves a half-written handle
    return path


def load_handle(cfg: Config, root: Path, unit: int) -> Handle | None:
    path = handle_path(cfg, root, unit)
    if not path.exists():
        return None
    return Handle.from_json(path.read_text(encoding="utf-8"))


def delete_handle(cfg: Config, root: Path, unit: int) -> None:
    handle_path(cfg, root, unit).unlink(missing_ok=True)


# ── registry ─────────────────────────────────────────────────────────


def create(cfg: Config) -> Runner:
    """Resolve cfg.runner to an implementation. The ONLY place a runner name
    becomes behavior; everywhere else consumes the instance or its
    capabilities."""
    if cfg.runner == "local":
        from foreman.runner.local import LocalRunner

        return LocalRunner(cfg)
    if cfg.runner == "sprite":
        raise ForemanError(
            "runner 'sprite' ships in v2.1 (SpriteRunner); v2.0 supports: local"
        )
    if cfg.runner == "docker":
        raise ForemanError(
            "runner 'docker' ships in v2.2 (DockerRunner); v2.0 supports: local"
        )
    raise ForemanError(f"unknown runner '{cfg.runner}' (known: local, sprite, docker)")
