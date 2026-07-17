# Foreman

Deterministic supervisor for dispatching agent work across local, Docker, and Sprite runners.

[![Build & Validate](https://github.com/ponderousdev/foreman/actions/workflows/build.yml/badge.svg)](https://github.com/ponderousdev/foreman/actions/workflows/build.yml)
[![Devcontainer Build](https://github.com/ponderousdev/foreman/actions/workflows/devcontainer-build.yml/badge.svg)](https://github.com/ponderousdev/foreman/actions/workflows/devcontainer-build.yml)
[![Open in Dev Containers](https://img.shields.io/static/v1?label=Dev%20Containers&message=Open&color=blue&logo=visualstudiocode)](https://vscode.dev/redirect?url=vscode://ms-vscode-remote.remote-containers/cloneInVolume?url=https://github.com/ponderousdev/foreman)
[![Renovate](https://img.shields.io/badge/maintained%20with-renovate-blue?logo=renovatebot)](https://github.com/apps/renovate)
[![Copier](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/copier-org/copier/master/img/badge/badge-grayscale-inverted-border-orange.json)](https://github.com/copier-org/copier)

Author: Evan Harmon (evan@evanharmon.com) — generated from
[harmon-init](https://github.com/evanharmon1/harmon-init) on 2026-07-15.

## Architecture

TODO: Summarize the architecture here. The full picture (with the Mermaid
diagram that must stay in sync with reality) lives in
[docs/architecture/README.md](docs/architecture/README.md).

## Setup & Installation

### Requirements

- [Homebrew](https://brew.sh) and [go-task](https://taskfile.dev)

### Install

```bash
task bootstrap   # one-time machine setup (Homebrew)
task install     # Brewfile deps + lefthook git hooks
task verify      # confirm everything passes
```

Or open the repo in the devcontainer (VS Code "Reopen in Container", or a
[Coder](https://coder.com) workspace) — the human profile lives in
`.devcontainer/dev/`, the AI/bot profile at `.devcontainer/`.

New here? Start with [docs/guides/onboarding.md](docs/guides/onboarding.md) and the
post-generation [docs/CHECKLIST.md](docs/CHECKLIST.md).

## Project Structure

```text
.
├── .claude/             # Claude Code settings + skills
├── .devcontainer/       # Dual-profile devcontainer (AI bot + dev/ human)
├── .github/             # Workflows, templates, CODEOWNERS, branch ruleset
├── src/foreman/         # The foreman package (Runner seam, dispatch, shepherd)
├── docs/                # Documentation (see docs/README.md)
├── scripts/             # Repo utility scripts (hygiene, status, summaries)
├── specs/               # Specifications
├── taskfiles/           # foreman:* task namespace (dogfood)
├── tests/               # Tests
├── AGENTS.md            # AI agent guidance (CLAUDE.md/GEMINI.md symlink here)
├── DESIGN.md            # Design / UX intent (AI-facing)
├── Taskfile.yml         # Task runner — single source of truth for commands
├── lefthook.yml         # Git hooks (delegate to Taskfile tasks)
└── todo.md              # Scratch todos (gitignored)
```

## Commands

`task` (or `task menu`) shows the interactive picker. Key targets:

| Command | What it does |
|---|---|
| `task verify` | Local merge gate: check + validate + guards |
| `task check` | All linters + typecheck (ruff, mypy, …) in parallel |
| `task foreman:plan` | Dry-run the supervisor: graph, waves, trust, capabilities |
| `task fix` | Auto-format, then lint |
| `task test` | Run tests (see [docs/architecture/tests.md](docs/architecture/tests.md)) |
| `task security` | gitleaks + dependency audit |
| `task release:patch` | Tag + GitHub release (also `:minor` / `:major`) |
| `task status` | Project status dashboard (also `status:git`/`:gh`/`:code`/`:env`) |
| `task status:setup` | Setup audit: GitHub config, toolchain, devcontainer, dev env |

## Testing

See [docs/architecture/tests.md](docs/architecture/tests.md). Tests live in `tests/`; CI runs the
same `task` targets as local hooks.

## CI/CD

| Workflow | Purpose |
|---|---|
| `build.yml` | lint, security → aggregate `verify` gate |
| `devcontainer-build.yml` | Prebuilds devcontainer images to GHCR on merge to main |
| `claude-plan/implement/review.yml` | `@claude plan` / `@claude implement` / `@claude review` on issues & PRs |
| `release.yml` | release-please maintains a release PR; merging it cuts the release |

Branch protection: `main` requires a PR with code-owner approval and the
`verify` + `security` checks (importable ruleset in `.github/`; see
[docs/architecture/branch-protection.md](docs/architecture/branch-protection.md)).
**Releases are intentional** — release-please keeps a rolling release PR from
conventional commits; merging it cuts the tag, GitHub release, and CHANGELOG.
Nothing auto-releases on a normal merge. `task release:*` stays as a manual
override.

## License

See [LICENSE](LICENSE).
