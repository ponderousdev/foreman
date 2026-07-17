"""Deterministic verification: execute the composed gate (#29) in the unit's
worktree — the agent's self-report is never trusted. Composition itself
lives in foreman.gate; this module only executes command lists locally.
Under sprite (v2.1) the same composed list executes in the guest instead —
composed by Foreman, never executed by it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from foreman.util import tail, utc_now_iso

VERIFY_TIMEOUT_MIN = 120


def run_gate(
    commands: list[list[str]], worktree: Path, unit_run_dir: Path
) -> tuple[bool, str, list[str] | None]:
    """Run the composed command list in order; stop at the first failure.

    Returns (ok, log tail, failed command). The complete log is at
    <unit_run_dir>/verify.log — failure prompts must name that path (#18).
    """
    log_path = unit_run_dir / "verify.log"
    with log_path.open("a", encoding="utf-8") as fh:
        for command in commands:
            fh.write(f"\n--- {utc_now_iso()} {' '.join(command)} ---\n")
            fh.flush()
            try:
                proc = subprocess.run(
                    command,
                    cwd=str(worktree),
                    stdout=fh,
                    stderr=subprocess.STDOUT,
                    timeout=VERIFY_TIMEOUT_MIN * 60,
                    check=False,
                )
                code = proc.returncode
            except subprocess.TimeoutExpired:
                fh.write(f"\nverify timed out after {VERIFY_TIMEOUT_MIN} minutes\n")
                code = 124
            except FileNotFoundError:
                fh.write(f"\nverify command not found: {command[0]}\n")
                code = 127
            if code != 0:
                return False, tail(log_path, 40), command
    return True, tail(log_path, 40), None
