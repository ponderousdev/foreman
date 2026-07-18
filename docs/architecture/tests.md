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
- `task verify` is the fast local gate; `task ci` is the full CI mirror.
- Tests never hit the network and never talk to real GitHub: the `Gh`
  transport is injected everywhere.
