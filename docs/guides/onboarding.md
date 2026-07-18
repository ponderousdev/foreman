# Onboarding

Getting productive in Foreman.

## Setup

1. Clone the repo: `git clone https://github.com/ponderousdev/foreman.git`
2. One-time machine setup (Homebrew): `task bootstrap`
3. Install dependencies and git hooks: `task install`
4. Verify everything works: `task verify`

Prefer the devcontainer? Open the repo in VS Code and "Reopen in Container"
(human profile: `.devcontainer/dev/`), or use a Coder workspace. See
[devcontainers.md](devcontainers.md) for local secrets (1Password Environments)
and Coder setup.

## Daily workflow

- Work on feature branches; direct commits to `main` are blocked.
- Conventional commit messages are enforced (`feat:`, `fix:`, `docs:`, ...).
- `task verify` before pushing; CI runs the same checks.
- Releases are intentional via release-please: merge the rolling release PR to
  publish (`task release:*` stays as a manual override).

## Working on Foreman itself

Foreman is a uv-managed, stdlib-only Python package under `src/foreman/`
(no runtime dependencies — the entire vendor surface is shelled out to `gh`,
`git`, and `backends/*.sh`). Get productive:

```bash
uv sync                 # create the venv from uv.lock
uv run foreman --help   # or: task foreman:plan -- --issue N
uv run pytest -q        # the hermetic suite (fake gh transport, no network)
```

Orient yourself around the seam and the loop:

- **The Runner seam** is `src/foreman/runner/` — `__init__.py` (the protocol,
  handle store, per-unit lock, `select()`) and `local.py` (LocalRunner). It
  must not leak: `tests/test_leak.py` forbids runner-name branches in policy
  code. Read [ADR 0003](../decisions/0003-foreman-v2-runner-seam.md) (D1–D14)
  before touching trust, capabilities, or the gate.
- **The loop** is `dispatch.py` → `shepherd.py` → `watch.py`; `graph.py`
  builds units/waves, `trust.py`/`capabilities.py`/`gate.py` gate them, and
  `github.py` is the entire (write-contract-guarded) GitHub surface.
- **The domain language** — unit, wave, arming, runner, capability, doneness —
  is in [../product/domain.md](../product/domain.md); use those names.

## Where things are

See [the docs map](../README.md) for all documentation, the
[root README](../../README.md) for usage + project structure, and
[../architecture/foreman.md](../architecture/foreman.md) for the deep dive.
