# 3. Foreman v2: the Runner seam, trust model, and distribution

Date: 2026-07-17

## Status

Accepted

Amends [ADR 0002](0002-foreman-deterministic-supervisor.md) (extraction:
Foreman is no longer template-shipped source). Decision source:
[`specs/foreman-v2.md`](../../specs/foreman-v2.md) — this ADR is the durable
home for its D1–D14; the spec keeps requirements and acceptance criteria.

## Context

Foreman v1 lived inside a Copier template and shipped by copying ~3,700
lines of source into every consumer; upgrades were merge conflicts. It also
had no boundary between the deterministic supervisor and the untrusted LLM
it dispatches: agents ran as subprocesses on Foreman's own box with an
inherited environment (`os.environ.copy()`). v2 extracts Foreman into this
standalone package and introduces a swappable Runner so one codebase can
execute a unit as a local subprocess (v2.0), an ephemeral Fly-Machine
"Sprite" microVM (v2.1), or a sibling Docker container (v2.2) — without the
choice leaking into graph, GitHub, or eligibility code.

## Decisions

- **D1 — The trust boundary is the container, not the process environment.**
  Locally, Foreman and its agents share one devcontainer; the write token is
  in PID 1's environment and in the bind-mounted env-file. An env allowlist
  is defense in depth, not containment — a `cat` defeats it. The v1-era
  invariant "the agent environment contains only the read-only token, even
  with a shared filesystem" is false as written and was deleted, not
  re-scoped.
- **D2 — Foreman runs only in the bot devcontainer.** No bare-host path: the
  bot HOME has no `op` CLI, no personal credentials, a scoped PAT; a
  personal HOME would change the trust story entirely. Enforced as a cheap
  startup tripwire (`FOREMAN_DEVCONTAINER=bot`, set in the bot profile's
  `devcontainer.json` `containerEnv` — deliberately not the shared
  Dockerfile, which the human profile also builds from). A guard against
  accident, not intent.
- **D3 — Local accepts relaxed separation.** Agents can reach the bot PAT;
  accepted deliberately for the supervised mode. The PAT's own scoping
  (selected repos; contents + pull_requests + issues; no workflow edit; no
  admin) is the real bound, together with branch protection and CODEOWNERS.
- **D4 — Local is trusted-input-only, enforced at plan time.** A repo is
  untrusted-input unless everyone who can create or edit its issues is a
  trusted actor: public ⇒ always untrusted; private ⇒ untrusted unless every
  account with access (collaborators, `affiliation=all`) appears in
  `trusted_actors`; unenumerable ⇒ fail closed. Planning injects the
  `untrusted-input` capability into `required_capabilities`; the ordinary
  hard-mismatch refusal does the rest. No runner-name branch in eligibility.
  Re-evaluated at dispatch; drift fails closed.
- **D5 — Sprites do not run Docker.** Policy, not discovery: one workload,
  one microVM, no daemon. Consequence: the `docker` capability has a real
  consumer on day one (local has it, sprite does not).
- **D6 — One agent image: the bot devcontainer.** `devcontainers/ci`
  publishes the resolved image; Fly boots OCI images, so the DinD binaries
  inside are inert weight, not a blocker. What does not translate is
  `devcontainer.json` runtime semantics (hooks, volumes, env-file) — that is
  the real Sprite work. Slim only after measurement, and only as a target in
  the same Dockerfile.
- **D7 — Capabilities are computed per environment, never per runner
  class.** `docker` is probed (daemon reachable?), `ports` is derived from
  `max_parallel` (the constraint is concurrency in a shared netns), and
  `untrusted-input` follows from the boundary by policy.
- **D8 — Sequencing is local → sprite → docker.** Public readiness rides the
  Sprite (v2.1); DockerRunner is an isolation-hygiene upgrade, last (v2.2).
  Sprite becomes the first isolated runner and carries that risk.
- **D9 — Ports at local concurrency 1: mechanism now, flip later.** The
  lone local agent could bind ports, but v2.0 withholds the advertisement:
  readiness races produce silent wrong work (an agent "fixing" working code
  because a server was not listening yet), and DinD children escape group
  kill. Every mechanism piece ships anyway; flipping later is a derived
  boolean, not a rename.
- **D10 — Public readiness moves to v2.1, behind the Sprite.** "Safe for
  public repositories" is earned by the VM boundary, not trusted-actor
  gating. Foreman dogfoods the composed gate on its own repo, whose `ci`
  genuinely needs a daemon.
- **D11 — Distribution is a git-tag `uvx` invocation, not a package
  index.** Consumers are not Python projects; nobody imports Foreman. The
  copier-owned wrapper Taskfile pins `FOREMAN_VERSION` and invokes
  `uvx --from git+https://github.com/ponderousdev/foreman@v{VERSION}
  foreman`. Going public later makes the same URL token-free; publishing to
  PyPI (optional, never a prerequisite) moves only the source expression.
  Requires a real console entry point and a plain PEP 517 backend, buildable
  from a git checkout. Rejected: GHCR (not a Python index), `uv tool
  install` in the image (kills independent pinning), a private index
  (infrastructure for what git+https does free).
- **D12 — DockerRunner is trusted-input-only.** Sibling containers run on
  the DinD daemon inside the privileged bot devcontainer; an escape lands
  where Foreman and the write token live. v2.2 buys isolation hygiene for
  trusted work, not an untrusted-input boundary. Revisiting requires real
  hardening and a new ADR, not a config flip.
- **D13 — Untrusted content classifies the unit; arming authorizes it.**
  The actor on the most recent arming-label event must be trusted — always,
  on every runner. Authorship and post-arming edits classify input: any
  untrusted contribution injects `untrusted-input` into the unit's required
  capabilities (refused under local naming sprite; dispatchable under
  sprite). Pinning stays absolute: a trusted post-arming edit refreshes the
  pin (a new attestation); an untrusted one breaks it — fail closed until a
  trusted actor re-arms, and the re-armed unit carries `untrusted-input`.
  This is what makes v2.1's untrusted dogfooding satisfiable, and it is
  what catches GitHub Apps (issue authors invisible to the collaborators
  API).
- **D14 — Version tags are immutable.** D11 makes a version tag executable
  distribution: whoever can move one chooses the code every consumer runs
  next, next to that consumer's own write token. `refs/tags/v*` carries two
  rulesets, split deliberately because bypass is ruleset-wide: creation
  (bypassable by the release path) lives apart from update+deletion (no
  bypass actors at all — moving a tag requires an admin to first edit the
  ruleset, an audit-logged act; a bad release is a new patch version, never
  a moved tag). Preflight probes all three controls empirically against
  scratch refs and the sacrificial `v0.0.0-probe` tag.

## Consequences

- The Runner protocol carries `UnitSpec` (incl. cmd + timeout), `kill`, and
  a liveness-checkable `Handle` whose exit status survives a Foreman
  restart (recorded by the spawn wrapper, never parent-only). `wait()` has
  a non-child mode; dead-with-no-status is abnormal, never guessed.
- A leak test enforces the seam: no runner-name branches outside the
  selection layer (`runner/`, `config.py`, `capabilities.py`, `cli.py`).
- The verify gate is composed from computed capabilities (baseline plus
  capability-keyed additions); there is no portable/full ladder. Where the
  gate executes is runner-conditional and security-relevant: under local,
  agent-authored branch commands run in Foreman's container (bounded by D4
  and the PAT); "no agent-authored branch command on Foreman's box" is a
  sprite-era invariant, not a universal one.
- Workflow changes are permanently human-only under this trust model (the
  PAT has no workflow write); the commit handoff detects workflow-touching
  diffs before push and fails the unit with that classification.
- v1's `preflight` (read-only issue analysis) is renamed `vet`; `preflight`
  now names the empirical security-assertion gate (#15).
- Explicit deferrals: `start`/`fetch`/`put`/`attach` (v2.1, Sprite-driven);
  local `ports` advertisement (D9); container pruning for DinD orphans
  (accepted residual until v2.2); per-unit `~/.claude` isolation (revisit
  if concurrency rises; a Sprite is one HOME per unit by construction).
