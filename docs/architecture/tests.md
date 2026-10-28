# Tests

How testing works in Foreman — including the runner test-tier decision
(issue #40) that keeps required CI daemon-free while still giving every
runner acceptance criterion a named home.

## Layers

| Layer | Tool | Command |
|---|---|---|
| Lint / static analysis | ruff, mypy, shellcheck, yamllint, markdownlint, actionlint | `task check` |
| Unit tests | pytest (hermetic; fake `gh` transport, no network) | `task test` |
| Application security | Semgrep CE | `task security:sast` |
| Optional second opinion | Snyk Code + Open Source, manual | `task security:sast:snyk` / `task security:sca:snyk` |
| Secrets | gitleaks | `task security:secrets` |
| Dependency audit | pip-audit over the exported uv.lock | `task security:audit` |

## Test tiers (issue #40 decision)

Two facts are in tension: Foreman's own required checks must stay fast and
daemon-free (#11), while runner acceptance criteria ultimately need a real
Docker daemon (v2.2) or a real Fly Machine (v2.1). One tier cannot satisfy
both, so the tiers are explicit:

### Tier 1 — required, on every PR (daemon-free, fast)

- `pytest` over `tests/`: the v1 suite (fake `gh` transport), the Runner
  protocol suite (mock runner), the **leak test** (no runner-name branches
  in graph/GitHub/eligibility code), capabilities/gate composition, trust
  classification, preflight probe logic against the fake transport, and
  LocalRunner mechanics (spawn wrapper, status recording, liveness,
  group-kill) — LocalRunner tests exercise real subprocesses but **no
  Docker daemon**: the docker capability probe is faked in tests.
- `ruff` + `mypy` via `task check`.
- These run in `.github/workflows/build.yml` and gate merges via the
  aggregated `verify` job.

### Tier 2 — environment-dependent, on demand (needs a Docker daemon)

- `task ci` locally adds `test:devcontainer:permissions` (and the
  devcontainer smoke tests exist as `test:devcontainer:*`); these shell out
  to the devcontainers CLI and need a daemon.
- LocalRunner's *live* docker probe (`capabilities()` returning `{"docker"}`
  inside the bot devcontainer) is verified here, not in Tier 1.

### Tier 3 — manual / future (v2.1+, real money or real VMs)

- SpriteRunner against a real Fly Machine (#30, #40): **decision** — CI does
  not boot Fly Machines in v2.0; when SpriteRunner lands, its
  integration run is a marked, manually triggered tier with an explicit
  spend cap, never a required PR check. Recorded here so #30 implements
  against a declared home rather than inventing one.

### What the mock runner does and does not prove

The mock Runner (tests) proves the *protocol shape*: dispatch drives
spawn → wait → verify → preserve/cleanup through the seam without caring
which runner is behind it. It proves nothing about process groups, exit
statuses, PID reuse, images, or isolation — those claims belong to the
LocalRunner subprocess tests (Tier 1) and the environment tiers above.
Never credit a runner acceptance criterion to the mock.

## Conventions

- Test files live in `tests/` at the repo root; shared fakes in
  `tests/fakes.py` (fake `gh` transport) — extend the fake, don't mock
  internals.
- `task verify` is the local definition-of-done gate; `task ci` is the full CI mirror.
- Tests never hit the network and never talk to real GitHub: the `Gh`
  transport is injected everywhere.
- CI runs the same task targets. The `repo-guards` job calls
  `ci:repo-guards` for the instruction audit, shared `test:suite`, and network
  skills check. Other gates have their own jobs (`check`,
  `devcontainer-permissions`, and Python `test`).

## CI classifier tests

`task test:ci-changes-classify` runs real Git fixtures for docs-only and mixed
changes, sensitive CI paths, rename/delete, symlink/mode changes, empty diffs,
missing history and invalid allowlists. A stale-base fixture proves that a
source revert hidden by the event base SHA still selects a full run against
the current base. Fixtures pin the shipped allowlist independently of the
consumer policy and separately test custom globs. Root `README.md` matches;
app Markdown under `src/pages/` and `src/content/`, `terraform/README.md` and
`.claude/` Markdown select a full run. A customized `docs/**` allowlist accepts
docs changes and rejects root Markdown. The test is a `test:suite` member.

`task test:ci-results` checks the aggregate separately: an authorized classifier
skip passes and is logged distinctly; a skip after classifier failure, with
`docs_only=false`, or on `push`/`merge_group` fails. Always-on jobs such as
`security` cannot borrow an app job's skip eligibility. Docs-only PRs still
run these tests through `repo-guards`.

## Parallel Task groups

Task runs the dependencies of `check` and `test:suite` concurrently by default.
Set Task's built-in `TASK_CONCURRENCY=1` environment variable to run dependencies
one at a time, for example `TASK_CONCURRENCY=1 task verify` or
`TASK_CONCURRENCY=1 task test:suite`.
