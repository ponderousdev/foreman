# Roadmap

Direction and planned work for Foreman. The long arc; day-to-day work lives in
GitHub issues and the [milestones](https://github.com/ponderousdev/foreman/milestones).
The authoritative design is [specs/foreman-v2.md](../../specs/foreman-v2.md).

## Now — v2.0 (the local runner)

Extraction to a standalone, versioned package plus the seam and the local
runner:

- Foreman is a `uvx`-installed CLI pinned by git tag — upgrades are a version
  bump, not a merge conflict.
- The **Runner seam** with `LocalRunner`: subprocess execution in the bot
  devcontainer, crash-safe handles, exit-status ground truth, timeout/kill.
- **Computed capabilities** and the **composed verify gate**; plan-time refusal
  of hard mismatches.
- **Trust model**: trusted-actor arming, D4/D13 classification, `local` is
  trusted-input-only, empirical `foreman preflight`.

## Next — v2.1 (the Sprite, and public readiness)

The boundary that makes untrusted content safe:

- **`SpriteRunner`** — one ephemeral Fly microVM per unit; `start` / `fetch` /
  `put` / `attach`, commits returned by `git bundle`, guest-level egress
  control.
- Dogfood against **untrusted issue content** under sprite, then take the repo
  public.
- Digest-pinned agent image; per-unit scoped credential delivery.

## Later — v2.2 (DockerRunner) and beyond

- **`DockerRunner`** — one sibling container per unit for local isolation
  hygiene (trusted input only; the container-next-to-a-root-daemon boundary is
  not the VM boundary).
- Candidates, not commitments: per-unit `~/.claude` isolation, `local@1`
  gaining `ports` (the full-fidelity local mode), a second agent vendor
  adapter.

## Non-goals (permanent)

Auto-merge (ever), running Foreman outside the bot devcontainer, Docker inside
a Sprite, a second agent image, HA / shared multi-developer instances. See the
spec's Non-goals for the full list and rationale.
