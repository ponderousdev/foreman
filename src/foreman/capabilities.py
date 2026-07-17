"""The capability model (D7, #28) and its plan-time enforcement.

Three capabilities decide what a unit can do — and what it may be given.
None is a property of a runner *class*: each is computed at runtime from the
environment (docker is probed, ports is derived, untrusted-input is set by
the boundary). Repositories declare `required_capabilities` (hard: missing
means refuse before dispatch); trust classification (D4/D13) injects
`untrusted-input` into a unit's requirements; the `[verify]` table keys
additions to capabilities (soft: missing means skip, Actions covers it).

Unknown capability names anywhere in config are errors at plan time — a
typo must never become a silently skipped check.

This module is part of the SELECTION layer (with runner/ and config.py):
it may know runner names, because refusals must name a compatible runner.
Policy code (graph/GitHub/eligibility) consumes only the sets and messages
produced here — the leak test enforces that split.
"""

from __future__ import annotations

from collections.abc import Iterable

from foreman.util import ForemanError

UNTRUSTED_INPUT = "untrusted-input"

KNOWN_CAPABILITIES: dict[str, str] = {
    "docker": "a usable Docker daemon is reachable from inside the unit",
    "ports": (
        "the unit may bind ports and run long-lived servers or a browser "
        "without colliding"
    ),
    UNTRUSTED_INPUT: (
        "the unit may be given untrusted content: the boundary is expected "
        "to contain a fully compromised agent, and no repository-write "
        "credential is reachable inside it"
    ),
}

# Planning-time policy view of runners that are not shipped yet: what each
# would advertise (by D5/D6/D12 policy), and the milestone that ships it.
# Used ONLY to name compatible runners in refusal messages — never to
# dispatch. LocalRunner is absent deliberately: what local advertises is
# computed live (probed docker, D9-withheld ports), not declared here.
_PLANNED_RUNNERS: dict[str, tuple[frozenset[str], str]] = {
    "sprite": (frozenset({"ports", UNTRUSTED_INPUT}), "v2.1"),
    "docker": (frozenset({"ports"}), "v2.2"),
}


def validate_names(names: Iterable[str], *, where: str) -> None:
    """Unknown capability names are config errors (spec: the composition
    stays honest)."""
    unknown = sorted(set(names) - set(KNOWN_CAPABILITIES))
    if unknown:
        raise ForemanError(
            f"{where}: unknown capability name(s): {', '.join(unknown)} "
            f"(known: {', '.join(sorted(KNOWN_CAPABILITIES))}). A typo must "
            "never become a silently skipped check."
        )


def refusal(required: set[str], advertised: set[str], runner_name: str) -> str | None:
    """The hard-mismatch refusal (#28), or None when requirements are met.

    Names every absent capability and the compatible runner(s) with their
    milestone; when nothing planned covers the gap either, says so."""
    missing = sorted(required - advertised)
    if not missing:
        return None
    # A suggested runner must satisfy the COMPLETE requirement set, not just
    # the part this runner was missing — otherwise required {docker, ports}
    # would recommend sprite, which does not advertise docker (D5).
    compatible = [
        f"{name} ({milestone})"
        for name, (caps, milestone) in _PLANNED_RUNNERS.items()
        if required <= caps
    ]
    message = (
        f"requires capability {', '.join(missing)} — not advertised by "
        f"runner '{runner_name}'"
    )
    if compatible:
        return message + f"; compatible runner: {', '.join(compatible)}"
    return message + "; no currently available runner advertises it"


def assert_repo_requirements(
    required: Iterable[str], advertised: set[str], runner_name: str
) -> None:
    """Target-level gate for `required_capabilities` — refuse before any
    dispatch work when the configured runner cannot satisfy the repo's hard
    requirements."""
    message = refusal(set(required), advertised, runner_name)
    if message:
        raise ForemanError(f"required_capabilities: {message}")
