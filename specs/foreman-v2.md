# Spec: Foreman v2 — the Runner seam

- **Status:** Approved
- **Owner:** Evan Harmon
- **Date:** 2026-07-15
- **Related:** [v2.0](https://github.com/ponderousdev/foreman/milestone/1) · [v2.1](https://github.com/ponderousdev/foreman/milestone/3) · [v2.2](https://github.com/ponderousdev/foreman/milestone/4) ·
  ADR 0002 in `ponderousdev/harmon-init`
  (`docs/decisions/0002-foreman-deterministic-supervisor.md`) ·
  supersedes the pre-extraction source document

> **On v1 code citations.** Foreman v1 does not live in this repo yet, so every
> `harmon-init/scripts/foreman/...` path below points into `ponderousdev/harmon-init`.
> #10 moves that source — and ADR 0002 — here, after which the qualifier drops and
> the line numbers shift. Anchor on the named function; line numbers are a
> convenience, not the reference.

## Problem / Why

Foreman v1 lives inside a Copier template (`harmon-init/scripts/foreman/`, ~3,700
lines across 18 modules) and ships by copying source into every consumer.
Upgrades are merge conflicts rather than version bumps.

It also has no boundary between the deterministic supervisor and the untrusted
LLM it dispatches. Agents run as subprocesses on Foreman's own box with an
inherited environment — `os.environ.copy()` in
`harmon-init/scripts/foreman/backend.py:103`. That is
acceptable for supervised work on trusted issues and wrong for anything else.

v2 extracts Foreman to its own package and introduces a swappable Runner, so one
codebase can execute a unit as a local subprocess or as an ephemeral Fly Sprite —
without the choice leaking into graph, GitHub, or eligibility code.

## Goal

Three things, in order:

1. Foreman is a versioned dependency, not vendored source.
2. Where a unit executes is a config flip behind one seam.
3. Dispatching against untrusted issue content is safe by construction — which
   requires a runner whose boundary is a VM, not a prompt convention.

## Non-goals

Carried from the milestone: Coder integration, a fourth runner, port allocation
or randomization, auto-merge (permanently — ADR 0002), HA, shared
multi-developer instances, and executing branch-defined Docker work on Foreman's
trusted box.

Added by this spec:

- **Running Foreman outside a container.** No bare-host / bare-laptop support.
  See [D2](#d2-foreman-runs-only-in-the-bot-devcontainer).
- **Docker inside a Sprite.** See [D5](#d5-sprites-do-not-run-docker).
- **A second agent image.** See [D6](#d6-one-agent-image-the-bot-devcontainer).
- **A named "single-threaded" local runner.** The capability is derived from
  `max_parallel`, not encoded in a runner name. See
  [D9](#d9-ports-at-local-concurrency-1-mechanism-now-flip-later).

## Architecture

### The seam

One `Runner` protocol; `local`, `sprite`, and `docker` implement it. Selection is
config. The seam must not leak: graph, GitHub, and eligibility code contain no
`cfg.runner` branches, enforced by a leak test. The only meaningful divergences
are *where* an agent executes and *how commits return to Foreman*.

Runner names describe **where execution happens** (ADR 0002). Concurrency,
isolation, and capability are properties *of* a runner, never names *for* one.

### Capabilities are two-dimensional, and probed

Two orthogonal properties decide what a unit can do. Neither is a property of the
runner *class* — both depend on the environment and how it is run, so
`capabilities()` probes rather than declares.

| Capability | Meaning |
|---|---|
| `docker` | A usable Docker daemon is reachable from inside the unit. |
| `ports` | The unit may bind ports and run long-lived servers or a browser without colliding. |

| Environment | `docker` | `ports` |
|---|---|---|
| local, `max_parallel > 1` | yes — DinD | no — agents share one netns |
| local, `max_parallel == 1` | yes | physically yes — **withheld in v2.0** ([D9](#d9-ports-at-local-concurrency-1-mechanism-now-flip-later)) |
| sprite | no — [D5](#d5-sprites-do-not-run-docker) | yes — one microVM per unit |
| docker (v2.2) | no — no socket for agents | yes — own netns per container |

**This table states what each environment *can* do; what a runner *advertises* is
narrower and is what you implement.** v2.0's LocalRunner returns `{"docker"}` and
nothing else, at any `max_parallel` — the concurrency-1 `ports` cell is physically
true and deliberately not advertised, per [D9](#d9-ports-at-local-concurrency-1-mechanism-now-flip-later).
Implement the advertisement, not the table.

The `ports` axis exists because the constraint it models is *concurrency inside a
shared network namespace*, not "local." A sprite is alone in its own VM; a lone
local agent is alone in the container. Both can bind :5173. Three parallel local
agents cannot.

### The verify gate is composed, not selected

There is no portable/full ladder and no `ci:portable`. The repo declares a
baseline plus capability-keyed additions; the gate runs the baseline and every
addition whose capability is present. Whatever does not run in-unit is GitHub
Actions' job, and the shepherd classifies red CI and dispatches a fix.

```toml
# .foreman.toml — consumer repo
required_capabilities = []          # hard requirement; missing => refuse before dispatch

[verify]
default = ["task", "verify"]        # check + build + test; needs nothing special
docker  = ["task", "verify:docker"] # additionally run when `docker` is present
ports   = ["task", "e2e"]           # additionally run when `ports` is present
```

Two mechanisms, two jobs:

- `required_capabilities` — **hard**. The baseline gate cannot run without it
  (e.g. unit tests use testcontainers). Missing → plan-time refusal naming the
  absent capability and a compatible runner.
- `[verify]` capability keys — **soft/additive**. Missing → skip it; Actions
  covers it.

A repo needing neither writes three lines and is done.

**Where the gate executes is runner-conditional, and that is security-relevant.**
Under local, Foreman's own process runs the branch's gate in the worktree — v1
behavior (`run_verify()` at `harmon-init/scripts/foreman/dispatch.py:286`) — and
the branch is agent-authored, so agent-authored commands execute inside Foreman's
container. That is co-location again ([D1](#d1-the-trust-boundary-is-the-container-not-the-process-environment)),
bounded by trusted input ([D4](#d4-local-is-trusted-input-only)) and the PAT's
scoping, not by isolation. Under sprite the gate runs in the guest and Foreman
never invokes it — there, "no agent-authored branch command on Foreman's box" is a
real invariant rather than a restatement of intent.

The original #7/#29 rule ("never execute agent-authored branch commands on
Foreman's trusted box") is therefore a **sprite-era invariant**. Writing it as
universal would assert something local cannot deliver.

### Trust model, per runner

**The boundary is the container (local) or the VM (sprite) — never the process
environment.** An env allowlist is defense in depth, not containment.

| | local (v2.0) | sprite (v2.1) |
|---|---|---|
| Boundary | the bot devcontainer | one Firecracker microVM per unit |
| Foreman / agent separation | none — same container, same HOME, same netns | full — separate machines |
| Agent reachable token | the bot PAT (accepted, [D3](#d3-local-accepts-relaxed-separation)) | read-only only |
| Input trust | **trusted actors only** ([D4](#d4-local-is-trusted-input-only)) | untrusted content permitted |
| Branch gate executes | in Foreman's container | in the guest VM |
| Commit handoff | Foreman reads the shared worktree | `git bundle` → Foreman's clone |

Local's blast radius is bounded by the **token**, not the env: a fine-grained PAT
scoped to selected repos, `contents` + `pull_requests` + `issues`, **no workflow
edit**, no admin — plus branch protection and CODEOWNERS. A fully compromised
local agent can push a branch, open a PR, and edit issues on those repos. It
cannot merge, and it cannot rewrite a workflow to reach Actions secrets.

That last control is load-bearing and must be **asserted**, not assumed (#15).

### Runner protocol

v2.0:

```python
class Runner(Protocol):
    def spawn(self, spec: UnitSpec) -> Handle: ...
    def wait(self, handle: Handle, timeout_s: int) -> ExitStatus: ...
    def kill(self, handle: Handle) -> None: ...
    def logs(self, handle: Handle) -> Iterator[str]: ...
    def exec(self, handle: Handle, cmd: list[str]) -> ExecResult: ...
    def preserve(self, handle: Handle) -> None: ...
    def cleanup(self, handle: Handle) -> None: ...
    def capabilities(self) -> set[str]: ...
```

`UnitSpec` carries image, workdir, env, **cmd** (v1 spawns `[adapter, "run"]` vs
`[adapter, "resume", ref]` — `spawn(image, workdir, env)` cannot express that),
limits, and timeout. `Handle` is opaque, serialized under `.foreman/runs/`, and
must be **liveness-checkable**: a bare PID is not a handle, because PIDs are
reused. Use PID + process start-time.

`wait(timeout_s)` and `kill()` are not new features — they restore v1 behavior
— in `harmon-init/scripts/foreman/backend.py`, `proc.wait(timeout=timeout_min * 60)`
at `:133` and `_kill_group()` at `:147`, with `timeouts.dispatch_min = 90` in
`.foreman.toml` — that the original #19 protocol silently dropped.

v2.1 adds, driven by the Sprite:

```python
    def start(self, handle: Handle) -> None: ...              # restore a preserved unit
    def fetch(self, handle: Handle, path: str) -> bytes: ...  # git bundle out
    def put(self, handle: Handle, path: str, data: bytes) -> None: ...  # prompt in
    def attach(self, handle: Handle) -> int: ...              # interactive TTY
```

`fetch`/`put` exist because the Sprite handoff has no transport otherwise:
`exec()` stdout over the Fly API is binary-unsafe and size-capped, and v1
delivers the prompt as `FOREMAN_PROMPT_FILE` — a host path that does not exist in
a guest.

## Milestone split

| | Scope | Ships |
|---|---|---|
| **v2.0** | Waves 1, 2, 3, 4, 5, 7 | Extraction, security controls, the seam, LocalRunner, capabilities |
| **v2.1** | Waves 8, 9 | SpriteRunner, untrusted-content dogfooding, public repo |
| **v2.2** | Wave 6 | DockerRunner — per-unit local isolation |

Rationale for the reorder: v2.0 is a coherent, shippable, useful tool. Public
readiness moves to v2.1 because the claim "safe for public repositories" is
earned by the Sprite's boundary, not by trusted-actor gating alone — and the
current graph would let the repo go public with only extraction and security
controls done. DockerRunner drops to last because it is a nice-to-have local
isolation upgrade, not a prerequisite for anything.

**Consequence to record explicitly:** #8's "start only after DockerRunner proves
the isolated-runner shape" and #30's `blocked-by: #25` are both dropped. Sprite
becomes the first isolated runner. The shared devcontainer image recovers part of
the lost local mirror; the lifecycle and transport are genuinely new and carry
that risk.

## Decisions

### D1: The trust boundary is the container, not the process environment

Locally, Foreman and its agents share one container. `GH_TOKEN` arrives via
`--env-file`, so it is in PID 1's environment *and* in
`.devcontainer/devcontainer.env` inside the bind-mounted workspace. An env
allowlist cannot contain what a `cat` reveals.

**Consequence:** #5's invariant ("even with a shared filesystem, the agent
environment contains only the read-only token") is **false as written and must be
deleted**, not re-scoped. #13's criteria ("agent `git push` is denied") apply to
sprite only.

### D2: Foreman runs only in the bot devcontainer

No bare-host support. Everything above is bounded because the bot devcontainer's
HOME is the *bot's*: no `op` CLI by design, no Tailscale, a scoped PAT. On a
personal machine HOME holds SSH keys, personal `gh` auth, 1Password, and a
`~/.claude/settings.json` whose `env` block can inject an API key. Same code,
entirely different trust story.

Building the bare-host path would defeat the protections that justify the tool.
Do not build it and do not present it as an option.

### D3: Local accepts relaxed separation

Agents can reach the bot PAT. Accepted deliberately: local is the pragmatic,
supervised mode, and the PAT's own scoping is the real bound (see
[trust model](#trust-model-per-runner)).

A separate UID plus permissions on the env-file would restore the split and
remains available later. It is not v2.0 work.

### D4: Local is trusted-input-only

Follows from D1 and D3. Untrusted issue content requires a boundary local does
not have. This must be **enforced at plan time**, not documented — nothing today
stops `runner = local` against a public repo.

### D5: Sprites do not run Docker

Decided by policy rather than discovery, which is cheaper than a spike. Use
Sprites as intended: one workload, one microVM, no daemon.

**Consequence:** the `docker` capability has a real consumer on day one (local
has it, sprite does not), so Wave 7 is not speculative machinery. Docker-gated
checks are GitHub Actions' job, per the composed gate.

### D6: One agent image (the bot devcontainer)

`devcontainers/ci` already resolves `devcontainer.json` (features and all) and
pushes `ghcr.io/ponderousdev/foreman-devcontainer` on every push to main, built
on `ubuntu-latest`. That is a plain OCI image, built amd64, which
neutralizes #30's arm64 footgun for free.

Fly boots OCI images as a microVM root filesystem — a Docker image is a
*packaging format*, not a runtime. The DinD binaries inside are **inert weight,
not a blocker**: nothing runs devcontainer lifecycle hooks on Fly, so dockerd
never starts. That is exactly D5's outcome, obtained by doing nothing.

What does **not** translate is `devcontainer.json`'s *runtime* semantics —
lifecycle hooks, the six named volumes, `--env-file`, `--shm-size`, privileged.
None are properties of the image. That gap is the real Sprite work.

Slim only after measurement (#33 already mandates this). If boot time forces it,
add a slim target to the *same* Dockerfile — never a separately maintained image,
which would duplicate ~15 `# renovate:`-pinned versions and drift on the first
bump.

### D7: Capabilities are probed and two-dimensional

See [above](#capabilities-are-two-dimensional-and-probed). The original #28
(`Local and Sprite advertise an empty set; Docker advertises docker`) is
backwards on every axis under D5 and D6.

### D8: Sequencing is local → sprite → docker

See [milestone split](#milestone-split).

### D9: Ports at local concurrency 1: mechanism now, flip later

A lone local agent has nothing to collide with and could use a browser. Real and
valuable — but deferred, because:

- Every piece of the mechanism is required anyway: `capabilities()` for `docker`,
  `kill` to restore v1's timeout, a capability-conditional preamble for v2.1.
  The incremental cost is one derived boolean.
- The *behavior* is where the risk lives. Readiness races produce **silent wrong
  work** — an agent pointing a browser at a not-yet-listening port concludes its
  code is broken and "fixes" working code. No `EADDRINUSE`, no signature to
  match, a plausible bad commit.
- Process-group kill does not fully mitigate: local has DinD, so an agent's
  `docker run -d` is a child of dockerd and escapes the group entirely.
- Sprites deliver this properly in v2.1 — per unit, no concurrency sacrifice, no
  orphans, because the VM stops.

**v2.0 therefore ships:** `capabilities()`, `kill` + timeout, and the conditional
preamble. **v2.0 does not ship:** LocalRunner advertising `ports`, kill-on-normal-exit,
or container pruning. A repo declaring `required_capabilities = ["ports"]` gets
an honest plan-time refusal — the gap is visible, not swallowed.

Local@1 stays valuable after v2.1 as the only environment with `docker` *and*
`ports` together: the full-fidelity mode, no Fly bill. Flipping it on later is a
few lines, non-breakingly, because it is derived rather than named.

### D10: Public readiness moves to v2.1, behind the Sprite

Issue #9 requires dogfooding "against public, untrusted issue content," which
requires a runner safe for it. Foreman itself is the natural target: its own
`ci` genuinely needs a daemon (devcontainer smoke tests shell out to the
devcontainers CLI), so a sprite agent runs the composed gate, opens a PR, Actions
runs the docker-keyed checks, and the shepherd fixes red CI. That is the designed
loop exercised on the one repo that forces it.

### D11: Distribution is a git-tag `uvx` invocation, not a package index

**Consumers are not Python projects.** `omator`, `mowing-bidder-web`, and
`lawnomator-site` all carry `package.json` and no `pyproject.toml`; harmon-init
has none either. Nobody imports Foreman — it is a CLI. So "install a pinned
Foreman dependency" had no project to be a dependency *of*, and the
GHCR-vs-PyPI question was answering something nobody asked. Today's invocation
is `PYTHONPATH=scripts python3 -m foreman` — bare `python3`, no venv, no
dependency management, working only because the source is vendored.

The wrapper Taskfile pins a version and invokes through `uvx`:

```yaml
vars:
  # renovate: datasource=github-tags depName=ponderousdev/foreman
  FOREMAN_VERSION: 1.2.3
  FOREMAN: uvx --from git+https://github.com/ponderousdev/foreman@v{{.FOREMAN_VERSION}} foreman
```

Why:

- **The pin lands where copier already owns it.** `taskfiles/foreman.yml` is
  template output, so `copier update` bumps Foreman by rewriting a file it fully
  controls — #12's "bump without conflicts" then holds *by construction* rather
  than by hoping a merge stays clean.
- **Auth needs no new infrastructure.** `.devcontainer/scripts/post-create-common.sh`
  runs `gh auth setup-git` on the headless path (VS Code absent ⇒
  `REMOTE_CONTAINERS` unset), bridging `GH_TOKEN` → git. That is the same
  credential path `worktree.py:push()` already depends on — it calls `git push`
  with no auth handling of its own.
- **uv and Renovate are already present.** uv is pinned in the devcontainer
  Dockerfile; `renovate.json` already runs three custom regex managers of exactly
  this shape.
- **It survives publication unchanged in shape.** Private:
  `git+https://…@v1.2.3`. Public: `uvx foreman@1.2.3`. One line, no consumer
  migration — which matters because [D10](#d10-public-readiness-moves-to-v21-behind-the-sprite)
  makes public a *when*, not an *if*.

Rejected: **GHCR** cannot serve wheels to a Python installer — it hosts OCI
artifacts, not a package index. **`uv tool install` in post-create** puts the pin
in the image, so consumers cannot pin independently and copier cannot bump it,
which loses the point. **A private index** is infrastructure to run and pay for,
solving what git+https solves for free.

**Prerequisite:** the bot PAT's selected-repo list must include
`ponderousdev/foreman`, or consumers cannot fetch it. That widens
[D3](#d3-local-accepts-relaxed-separation)'s blast radius — an agent in a
consumer repo could open a PR against foreman. Deliberate and small, but a
decision rather than a side effect.

**Consequence for #10:** Foreman needs a real `[project.scripts]` console entry
point and a plain PEP 517 backend, **buildable from a git checkout**. The
invocation changes from `PYTHONPATH=scripts python3 -m foreman` to `foreman`,
which is a break the wrapper Taskfile (#12) owns.

## Requirements

### v2.0

- [ ] Foreman is a standalone uv-managed package with preserved history, its own
      semver, and a thin harmon-init integration.
- [ ] Foreman installs into a **non-Python** consumer via `uvx` from a git tag
      ([D11](#d11-distribution-is-a-git-tag-uvx-invocation-not-a-package-index)),
      with the pin in the copier-owned wrapper Taskfile and a console entry point.
- [ ] Foreman runs only in the bot devcontainer; no bare-host path exists.
- [ ] The Runner protocol carries `UnitSpec` (incl. cmd + timeout), `kill`, and a
      liveness-checkable `Handle`. A mock Runner satisfies it.
- [ ] Graph, GitHub, and eligibility code contain no runner branches (leak test).
- [ ] LocalRunner: subprocess in the unit worktree, allowlisted env, exit-code
      ground truth, preserve-on-failure, `capabilities() == {"docker"}`.
- [ ] `capabilities()` is probed; the verify gate is composed from it;
      `required_capabilities` mismatches are refused before dispatch.
- [ ] Trusted-actor gating validates author, arming actor, and post-arming
      editors; the trusted input surface is defined; content is pinned against
      TOCTOU.
- [ ] `runner = local` against a repo with untrusted contributors is refused at
      plan time.
- [ ] Preflight asserts login, ruleset-requires-PR, read-token cannot write, and
      **write-token cannot edit workflows** — all non-destructively.
- [ ] Per-unit lock + liveness probe prevent double-dispatch after a crash.
- [ ] The Foreman v2 architecture ADR is landed.

### v2.1

- [ ] SpriteRunner boots the pinned devcontainer image on Fly, execs, returns an
      exit code, and stops (not destroys) on failure.
- [ ] `fetch`/`put`/`start`/`attach` land; Sprite commits arrive by `git bundle`
      and are pushed only by Foreman.
- [ ] `capabilities() == {"ports"}`; guest-level egress control ships.
- [ ] Sprite credential delivery is defined and proven before the v2.1 trust
      contract is claimed: scoped read-only, no host-environment inheritance, not
      persisted into the image or a committed file, and asserted by preflight.
- [ ] The devcontainer image is digest-pinned and versioned.
- [ ] Foreman is dogfooded against untrusted content, then published.

### v2.2

- [ ] DockerRunner: one sibling container per unit, no socket, no write token.

## Acceptance criteria (Given / When / Then)

### Scenario: the composed gate skips what the environment lacks

- **Given** a repo with `[verify] default`, `docker`, and `ports` keys
- **When** a unit runs under `runner = local` with `max_parallel = 3`
- **Then** Foreman runs `default` and the `docker` command, skips the `ports`
  command, and Actions remains the authority for it.

### Scenario: a hard capability mismatch is refused before dispatch

- **Given** a repo with `required_capabilities = ["ports"]`
- **When** planning under `runner = local`
- **Then** the plan refuses, names `ports` as absent, and names no currently
  compatible runner (until v2.1).

### Scenario: local refuses untrusted input

- **Given** a repo whose contributors are not all trusted actors
- **When** planning under `runner = local`
- **Then** the plan refuses and names sprite as the compatible runner.

### Scenario: every preflight assertion is proven, not assumed

Fine-grained token permissions cannot be introspected, so each control below is
probed empirically. All four must pass before any dispatch.

- **Given** the write token, the read token, and a ruleset requiring pull requests
- **When** `foreman:preflight` runs
- **Then** the write token's authenticated login matches `expected_login`
- **And** the applicable ruleset is confirmed to require pull requests for the
  write actor, and a bypass-capable write actor fails
- **And** the read token is proven unable to write
- **And** the write token is proven unable to edit workflows — the control
  [D3](#d3-local-accepts-relaxed-separation)'s relaxed separation leans on
- **And** every probe is non-destructive: denial is the expected path, and an
  unexpected success fails loudly *instead of* completing the write it was
  testing for — a probe that succeeds must not be the thing that does damage.

### Scenario: a timed-out agent's process group is terminated

- **Given** a unit exceeding `timeouts.dispatch_min`
- **When** the timeout fires
- **Then** `kill()` terminates the agent's process group, the worktree and session
  are preserved, and the unit is reported as timed out — matching v1 behavior.
- **And** the report says *the process group was terminated*, not that the unit was
  fully stopped: daemon-level descendants survive (see
  [Notes](#notes)). Do not claim more than was done.

### Scenario: a crash does not double-dispatch

- **Given** a mid-wave crash with a live agent and a persisted handle
- **When** Foreman reruns
- **Then** it re-derives state from GitHub and git, probes handle liveness
  (PID + start-time), and reattaches rather than redispatching.

## Open questions

- **Secrets delivery to a Sprite.** There is no host, no `initializeCommand`, and
  no `op` CLI in the bot profile. Fly secrets set by Foreman at Machine-create
  time is the obvious answer; confirm it.
- **Devcontainer image boot time on Fly.** Measure before touching #33. The
  build workflow reclaims ~13GB of runner disk, so the image is large.
- **Runner test strategy.** Does CI boot a real Fly Machine, or is that a marked
  manual tier?
- **Does the bot PAT's selected-repo list need narrowing before v2.0 dispatches?**
  It is the blast radius of D3.

## Notes

**This document's lifecycle.** It currently does three jobs that
[`specs/README.md`](README.md) layers apart, deliberately, while the design is
still moving — splitting now would create three copies to drift. Once the design
settles: D1–D10 become the architecture ADR that #19 already requires (immutable;
superseded, never edited), the architecture sections move to `docs/architecture/`
(living; they describe what *is*), and this spec keeps only requirements and
acceptance criteria — reaching `Status: Implemented` when v2.0 ships, and staying
in-tree as the record of what we set out to build.
The migration section that mapped this spec onto the pre-existing issues has
been removed now that it is applied — it was scaffolding, and the issues are
the durable record of what it said.

**Deferred with a known risk:** per-unit `~/.claude` isolation. Session
transcripts partition by cwd (`~/.claude/projects/<escaped-cwd>/<id>.jsonl`), so
worktree-per-agent does not collide. `~/.claude.json` is a single unlocked global
file written by every session, and corruption is reported under concurrency —
but the reports skew Windows, and no corruption has been observed here at
`max_parallel = 3` on Linux. Revisit if concurrency rises materially; the fix is
a per-unit `CLAUDE_CONFIG_DIR`. This is a LocalRunner-only problem — a Sprite is
one HOME per unit by construction.

**Accepted residual: daemon-level orphans survive `kill()`.** Local has DinD, so
anything an agent starts with `docker run -d` is a child of dockerd, not of the
agent — it escapes process-group termination entirely, at any concurrency, ports
or no ports. A timed-out unit can therefore leave a container running and writing
after Foreman reports it terminated, which is precisely the class of lying status
ADR 0002 exists to prevent. v2.0 does not prune, because Foreman cannot force an
agent to label its containers and snapshot-diffing the daemon is racy; the
acceptance criterion above is worded to claim only what is done. Bounded by
[D4](#d4-local-is-trusted-input-only) — this is agent misbehavior, not an attack —
and it disappears under sprite (the VM stops) and under v2.2's DockerRunner, where
per-unit containers make cleanup natural. Revisit there, not in v2.0.

**Accepted residual:** the bot devcontainer runs `--privileged` (the
docker-in-docker feature sets it), and privileged containers are escapable
independent of Docker. Bounded by D4 (trusted input) and by the fact that on
macOS and WSL2 the Docker host is already a VM, so an escape lands in
LinuxKit/WSL2 rather than on the machine itself.

**Pattern worth keeping in view:** local's three compromises — a reachable write
token, a shared HOME, and `~/.claude` contention — are all the same problem,
co-location. v2.1 does not mitigate them; it removes the cause.
