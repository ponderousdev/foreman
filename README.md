# Foreman

Deterministic supervisor that dispatches agent work from a GitHub milestone,
verifies it, opens a PR per unit, and shepherds those PRs to mergeable —
**humans merge, always.**

[![Build & Validate](https://github.com/ponderousdev/foreman/actions/workflows/build.yml/badge.svg)](https://github.com/ponderousdev/foreman/actions/workflows/build.yml)
[![Devcontainer Build](https://github.com/ponderousdev/foreman/actions/workflows/devcontainer-build.yml/badge.svg)](https://github.com/ponderousdev/foreman/actions/workflows/devcontainer-build.yml)
[![Open in Dev Containers](https://img.shields.io/static/v1?label=Dev%20Containers&message=Open&color=blue&logo=visualstudiocode)](https://vscode.dev/redirect?url=vscode://ms-vscode-remote.remote-containers/cloneInVolume?url=https://github.com/ponderousdev/foreman)
[![Renovate](https://img.shields.io/badge/maintained%20with-renovate-blue?logo=renovatebot)](https://github.com/apps/renovate)
[![Copier](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/copier-org/copier/master/img/badge/badge-grayscale-inverted-border-orange.json)](https://github.com/copier-org/copier)

Author: Evan Harmon (evan@evanharmon.com) — generated from
[harmon-init](https://github.com/evanharmon1/harmon-init) on 2026-07-15.

## What it is

Foreman reads a milestone's (or a single issue's) dependency graph from
GitHub, dispatches the currently-unblocked issues to isolated headless agents
through a swappable **Runner**, verifies each result with the repo's own gate,
opens one PR per unit, and keeps those PRs healthy (CI repair, review
adjudication, rebases) until a human merges them.

Scheduling, readiness, verification, doneness, and trust are **plain code**;
LLMs only produce artifacts that deterministic gates then measure. **No AI ever
merges** — the human merge is the only event that advances the graph.

Where a unit executes is a config flip behind one seam:

| Runner | Status | Boundary | Untrusted input? |
|---|---|---|---|
| `local` | **v2.0 (shipping)** | the bot devcontainer (subprocess) | no — trusted actors only |
| `sprite` | v2.1 | one ephemeral Fly microVM per unit | yes — the VM is the boundary |
| `docker` | v2.2 | one sibling container per unit | no |

Design intent: [DESIGN.md](DESIGN.md) · architecture:
[docs/architecture/foreman.md](docs/architecture/foreman.md) · decisions:
[ADR 0002](docs/decisions/0002-foreman-deterministic-supervisor.md) /
[ADR 0003](docs/decisions/0003-foreman-v2-runner-seam.md) · spec:
[specs/foreman-v2.md](specs/foreman-v2.md) · terms:
[docs/glossary.md](docs/glossary.md).

## How it works (the loop)

1. **plan** — read the milestone graph, resolve *waves* (units whose
   dependencies are all satisfied), and classify each unit's trust and
   capabilities. No side effects.
2. **dispatch** — for each ready unit: isolate a git worktree, assemble a
   deterministic prompt, run the agent through the Runner, verify with the
   composed gate in the worktree, and open a PR. Foreman opens PRs; the agent
   never pushes.
3. **shepherd** — keep open PRs healthy: retry environmental CI failures once,
   rebase cleanly, hand mechanical failures and review-bot threads back to a
   bounded agent, and label the green-and-mergeable ones `ready-to-merge` with
   a dependency-aware merge order.
4. **You merge.** The next tick re-derives state from GitHub + git and
   discovers the newly-unblocked wave. During a merge freeze Foreman idles by
   design.

Crash-safe by construction (state is re-derived every tick, never stored); a
timed-out unit's process group is killed and reported as such; trust is
enforced by the runner boundary, not a prompt — so **`local` is
trusted-input-only**, and untrusted issue content is refused at plan time until
a runner with the right boundary (sprite) exists.

## Using Foreman in a consumer repo

Foreman is a CLI installed from a pinned git tag — nothing to add to your
project's language manifest:

```bash
uvx --from git+https://github.com/ponderousdev/foreman@v2.0.0 foreman --help
```

In a repo scaffolded from harmon-init this is wrapped as `task foreman:*`, with
the version pinned in the copier-owned `taskfiles/foreman.yml` (so
`copier update` bumps it without merge conflicts). Migrating a repo that still
vendors Foreman source? See
[docs/guides/foreman-migration.md](docs/guides/foreman-migration.md).

### 1. Configure `.foreman.toml`

At the repo root. Plan-affecting keys (`runner`, `trusted_actors`,
`required_capabilities`, `[verify]`) are read from the **default branch** — an
agent can't edit its own trust or its own gate.

```toml
runner = "local"                      # local (v2.0) | sprite (v2.1) | docker (v2.2)
expected_login = "your-bot"           # identity asserted before any write
trusted_actors = ["you", "your-bot"]  # who may arm, and whose content is trusted input

# The composed verify gate: a baseline plus capability-keyed additions. Foreman
# runs the baseline and every addition whose capability is present; whatever
# doesn't run in-unit is GitHub Actions' job.
[verify]
default = ["task", "verify"]          # runs everywhere — needs no capability
docker  = ["task", "verify:docker"]   # additionally when `docker` is present

required_capabilities = []            # hard requirements; a mismatch is refused at plan time
```

Full reference: [docs/architecture/foreman.md](docs/architecture/foreman.md).

### 2. Arm the issues you want worked

Foreman only dispatches an issue a **trusted actor** has explicitly armed —
via the org-level `foreman` issue field, or `foreman:*` labels on
personal-account repos:

- `foreman:approved` (or a backend, e.g. `foreman:claude`) — arm this issue.
- `foreman:hold` — never dispatch.

Every dispatchable issue needs an `## Acceptance Criteria` section; items
tagged `[HUMAN]` are surfaced for a human and never attempted. Untrusted
authorship classifies the unit as `untrusted-input`, which `local` refuses.

### 3. Run the loop

```bash
foreman preflight              # assert the security controls empirically (once per setup/rotation)
foreman plan     --milestone 1 # dry run: waves, trust, capabilities — no side effects
foreman dispatch --milestone 1 # dispatch ready units → verify → open PRs
foreman shepherd               # keep open foreman PRs healthy
foreman watch    --milestone 1 # loop plan→dispatch→shepherd with heartbeats (stop file: .foreman-stop)
foreman status   --milestone 1 # read-only snapshot + the human-action queue
```

Also: `foreman vet` (read-only agent analysis of the target that drafts
correction comments for human approval), `foreman retry --unit N`,
`foreman attach --unit N` (resume a preserved failed unit in place),
`foreman cleanup`. Any target takes `--milestone <n|title>` or `--issue <n>`.

## Setup

### Operator setup (before first dispatch)

Foreman runs **only in the bot devcontainer**; in `local` mode its blast radius
is bounded by token scoping, not isolation. Before dispatching:

- **Two tokens.** A write PAT (`GH_TOKEN`) for Foreman — `contents` +
  `pull_requests` + `issues`, **no workflow edit, no admin** — and a separate
  read-only PAT (`FOREMAN_AGENT_GH_TOKEN`) handed to agents.
- **Branch protection.** `main` requires a PR, code-owner review, and the
  `verify` + `security` checks (importable ruleset in `.github/`).
- **Immutable version tags.** `refs/tags/v*` under the two tag rulesets
  (`.github/`) before any consumer pins a tag — a moved tag is code execution
  in every consumer.
- **Green preflight.** `foreman preflight` probes all of the above empirically
  (login, PR rule, read-token-can't-write, no-workflow-edit, tag immutability).

See [docs/architecture/security.md](docs/architecture/security.md) and
[docs/architecture/branch-protection.md](docs/architecture/branch-protection.md).

### Developing this repo

Requirements: [Homebrew](https://brew.sh) + [go-task](https://taskfile.dev), or
open in the devcontainer (VS Code "Reopen in Container", human profile
`.devcontainer/dev/`; or a [Coder](https://coder.com) workspace).

```bash
task bootstrap   # one-time machine setup (Homebrew)
task install     # Brewfile deps + lefthook git hooks
task verify      # fast local gate — confirm everything passes
```

Foreman itself is a uv-managed Python package; commands route through the
Taskfile (`task test`, `task check`, `task foreman:*`), which runs uv under
the hood and syncs the venv automatically. New here? Start with
[docs/guides/onboarding.md](docs/guides/onboarding.md) and the post-generation
[docs/CHECKLIST.md](docs/CHECKLIST.md).

## Project structure

```text
.
├── .claude/             # Claude Code settings + skills + foreman-* agents
├── .devcontainer/       # Dual-profile devcontainer (AI bot + dev/ human)
├── .github/             # Workflows, templates, CODEOWNERS, branch + tag rulesets
├── src/foreman/         # The foreman package
│   ├── runner/          #   the Runner seam (protocol, handle store, LocalRunner)
│   ├── dispatch.py      #   per-unit pipeline; shepherd.py, watch.py, graph.py, …
│   ├── trust.py         #   D4/D13 trust; capabilities.py, gate.py, preflight.py
│   └── backends/        #   agent-adapter shell scripts (claude.sh, mock.sh)
├── docs/                # Documentation (see docs/README.md)
├── scripts/             # Repo utility scripts (hygiene, status, summaries)
├── specs/               # Specifications (specs/foreman-v2.md)
├── taskfiles/           # foreman:* task namespace (dogfood)
├── tests/               # Tests (hermetic; fake gh transport)
├── AGENTS.md            # AI agent guidance (CLAUDE.md/GEMINI.md symlink here)
├── DESIGN.md            # Design / UX intent (AI-facing)
└── Taskfile.yml         # Task runner — single source of truth for commands
```

## Commands

`task` (or `task menu`) shows the interactive picker. Key targets:

| Command | What it does |
|---|---|
| `task check` | Fast gate: all linters + typecheck (ruff, mypy, …) in parallel |
| `task verify` | Definition-of-done gate: check + validate + guards + tests |
| `task ci` | Full CI mirror (verify + security + devcontainer assert) |
| `task fix` | Auto-format, then lint |
| `task test` | Run tests (see [docs/architecture/tests.md](docs/architecture/tests.md)) |
| `task security` | Free local baseline: Semgrep CE + gitleaks + Python dependency audit |
| `task security:sast` / `security:sca` | Semgrep CE / Python dependency audit |
| `task security:sast:snyk` / `security:sca:snyk` | Optional Snyk second-opinion scans (manual or explicitly scheduled) |
| `task foreman:plan` | Dry-run the supervisor: graph, waves, trust, capabilities |
| `task release:patch` | Tag + GitHub release (also `:minor` / `:major`) |
| `task status` | Project status dashboard (also `status:git`/`:gh`/`:code`/`:env`) |

## Testing

See [docs/architecture/tests.md](docs/architecture/tests.md) for the test tiers.
The suite is hermetic (fake `gh` transport, no network); CI runs the same `task`
targets as local hooks. Required checks are daemon-free; docker/Fly-backed
runner checks are a separate tier.

## CI/CD

| Workflow | Purpose |
|---|---|
| `build.yml` | lint, test, security → aggregate `verify` gate (required checks) |
| `devcontainer-build.yml` | Prebuilds devcontainer images to GHCR on merge to main |
| `claude-plan/implement/review.yml` | `@claude plan` / `implement` / `review` on issues & PRs |
| `release.yml` | release-please maintains a release PR; merging it cuts the release |

Branch protection: `main` requires a PR with code-owner approval and the
`verify` + `security` checks (importable ruleset in `.github/`; see
[docs/architecture/branch-protection.md](docs/architecture/branch-protection.md)).
**Releases are intentional** — release-please keeps a rolling release PR from
conventional commits; merging it cuts the tag, GitHub release, and CHANGELOG.
Nothing auto-releases on a normal merge. `task release:*` stays as a manual
override.

## Dogfood

Foreman runs on its own repository: it dispatches, verifies, and shepherds
Foreman's own issues through the same pipeline it ships. This very section was
added by a Foreman-dispatched agent working
[issue #72](https://github.com/ponderousdev/foreman/issues/72) — the end-to-end
smoke test of the v2.0 dispatch pipeline.

## Support posture

Personal tool, published as-is; issues/PRs may go unanswered. Security
reports go through [SECURITY.md](.github/SECURITY.md), which is monitored.

## License

Apache-2.0 — see [LICENSE](LICENSE).
