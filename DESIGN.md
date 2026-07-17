# DESIGN.md

AI-facing statement of design intent for **Foreman**. This file carries the
*why* and the prose rules that code and config can't encode — read it before
making design, UX, or structural decisions. `AGENTS.md` covers how to work in
the repo; this covers what "good" looks like for the product itself.

## Purpose

Foreman is a **deterministic supervisor** that dispatches agent work across
local, Sprite, and Docker runners. It reads a milestone's dependency graph
from GitHub, dispatches unblocked issues to isolated headless agents, verifies
each result with the repo's own gate, opens PRs, and shepherds them to
mergeable — and **humans do the merging, always**.

The single most important quality is **honest status**: Foreman must never
report more than it did. A supervisor that spawns paid VMs from LLM decisions
and claims "done" when it isn't is worse than no supervisor. Every place the
design chooses the more conservative, more auditable, more truthful option
over the more capable one, that is the purpose being served.

## Principles

- **Deterministic supervisor, not an LLM orchestrator.** Scheduling,
  readiness, verification, doneness, and trust are plain code. LLMs produce
  artifacts (code, replies, analyses) that deterministic gates then measure.
  Zero tokens on coordination.
- **The boundary is the container or the VM, never a prompt.** Trust is
  enforced by where an agent runs and what credentials it can reach — not by
  telling it to behave. An env allowlist is defense in depth, not containment.
- **Inputs are stored; derived state is never stored.** A stored status lies
  after a crash; state re-derived from GitHub + git cannot. Handles under
  `.foreman/` are a reattachment cache, not state of record.
- **Claim only what was done.** A timed-out unit's process group was
  terminated — daemon descendants may survive, and the report says so. A dead
  process with no recorded status is abnormal, never guessed into an exit
  code. Withheld capabilities are named at plan time, not silently skipped.
- **Clarity over cleverness.** Optimize for the next reader (often an AI
  agent). Reuse existing patterns before inventing new ones.

## Non-negotiables

When a non-negotiable lives in code or config, that file wins if they
disagree.

- **No AI ever merges to `main`. Ever.** No auto-merge, no enabling flag —
  permanently ([ADR 0002](docs/decisions/0002-foreman-deterministic-supervisor.md)).
  Enforcement is server-side (branch ruleset, code-owner review, a bot token
  without bypass or workflow-edit). Foreman opens the PR, reports it green,
  and stops.
- **The Runner seam must not leak.** Graph, GitHub, and eligibility code carry
  no runner-*name* branches — they consume advertised **capabilities**.
  `tests/test_leak.py` enforces this; a runner name outside the selection
  layer (`runner/`, `config.py`, `capabilities.py`, `cli.py`) is a bug.
- **Local is trusted-input-only.** Untrusted issue content requires a boundary
  local does not have. This is enforced at plan time via the `untrusted-input`
  capability ([ADR 0003](docs/decisions/0003-foreman-v2-runner-seam.md) D4/D13),
  not merely documented.
- **Plan-affecting config is read from the default branch.** `runner`,
  `trusted_actors`, `required_capabilities`, and `[verify]` come from
  Foreman's own clone — an agent must never edit its own trust or its own gate.
- **Foreman runs only in the bot devcontainer** (D2). No bare-host path exists
  and none may be added.
- **Version tags are immutable** (D14). Distribution rides on
  `git+https://…@vX.Y.Z`, so a moved tag is code execution in every consumer.
- **Never write to a credential store unprompted** (see `AGENTS.md` Hard
  Rules).

## Decisions & when to deviate

The immutable v2 decisions (D1–D14) live in
[ADR 0003](docs/decisions/0003-foreman-v2-runner-seam.md); the living
requirements and acceptance criteria in
[`specs/foreman-v2.md`](specs/foreman-v2.md). Significant or hard-to-reverse
choices get a new ADR — supersede, never silently drift. Deviating from a
rule here is allowed when justified; record *why* in an ADR.
