# Vision

What Foreman is, who it is for, and why it exists.

## Problem

A well-specced milestone is a graph of issues with dependency edges. Working
them one at a time is safe but slow; turning a swarm of agents loose on them at
once is chaos. Real milestone runs surfaced the failure modes: stored status
fields lie after a crash, stacked PRs create rebase cascades, subscription
usage windows kill unattended agents, review-bot round-trips eat half the
effort — and "the agent says it's done" is not a verification strategy. And
once agents run against issues anyone can file, "safe" stops being a prompt
convention and becomes a boundary problem.

## Where we are headed

A supervisor you can point at a milestone and trust to make **honest, bounded
progress without babysitting each step** — and without ever taking the one
action that must stay human: the merge. The outcome we want is a maintainer
reviewing and merging a steady queue of verified, self-contained PRs, instead
of orchestrating agents by hand. Correctness and safety come from deterministic
code and real isolation boundaries, not from asking an LLM to behave.

## Principles

- **Deterministic supervisor, not an LLM orchestrator.** Scheduling, readiness,
  verification, doneness, and trust are plain code. Zero tokens on coordination.
- **The human merge is the only graph-advancing event — permanently.** No
  auto-merge, no enabling flag; enforced server-side.
- **Honest status.** Claim only what was done: a killed process group is
  reported as a killed process group; a dead unit with no recorded exit status
  is abnormal, never guessed.
- **Boundaries over prompts.** Trust is enforced by where an agent runs and what
  credentials it can reach — the container (local) or the VM (sprite), never a
  prompt convention.
- **Inputs are stored; state is derived.** Re-derived from GitHub + git every
  tick, so crashes and multi-day runs are safe by construction.

## Success looks like

- A milestone advances to done through a queue of verified PRs, with the human
  spending their time on **merge decisions**, not agent wrangling.
- A crash, reboot, or laptop sleep loses nothing — the next tick re-derives
  reality.
- Dispatching against untrusted issue content is safe *by construction* (the
  sprite boundary), not by hoping a prompt holds.
- Foreman upgrades in a consumer are a version bump, not a merge conflict.

## Who it is for

Maintainers running milestone-driven delivery with agent help who want
throughput **without** surrendering the merge gate or the security boundary —
starting with this project's own ecosystem (harmon-init-scaffolded repos), and
generalizing to any repo that can express work as a dependency-linked milestone.
