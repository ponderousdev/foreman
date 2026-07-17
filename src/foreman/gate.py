"""The composed verify gate (#29): baseline plus capability-keyed additions.

There is no portable/full ladder and no ci:portable. The repo declares a
baseline (`[verify] default`) plus capability-keyed additions; the gate runs
the baseline and every addition whose capability is advertised, in
declaration order. Whatever does not run in-unit is GitHub Actions' job, and
the shepherd classifies red CI and dispatches a fix.

Composition is Foreman-owned and deterministic in every mode. Runners differ
only in WHERE the composed list executes: under local, Foreman's own process
runs it in the worktree (v1 behavior — and therefore agent-authored branch
commands execute inside Foreman's container: co-location, D1, bounded by
trusted input, D4, and the PAT's scoping); under sprite the list travels to
the guest on UnitSpec.gate_cmds (#30) and the adapter executes it there.
"No agent-authored branch command on Foreman's box" is a sprite-era
invariant — never claim it for local.

Unknown [verify] keys were already refused at config load; this module
assumes a validated table.
"""

from __future__ import annotations

from foreman.config import Config


def compose(cfg: Config, advertised: set[str]) -> list[list[str]]:
    """Baseline first, then capability-keyed additions in declaration order
    (tomllib preserves it). Additions whose capability is absent are
    skipped — soft by design; Actions covers them."""
    commands: list[list[str]] = [list(cfg.verify["default"])]
    for key, command in cfg.verify.items():
        if key == "default":
            continue
        if key in advertised:
            commands.append(list(command))
    return commands


def describe(commands: list[list[str]]) -> str:
    """Human-facing one-liner for prompts and failure details."""
    return " && ".join(" ".join(command) for command in commands)
