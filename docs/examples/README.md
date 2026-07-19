# Examples

> **These are illustrations, not shipped defaults.** Foreman ships a *generic*
> baseline (`[verify] default = ["task", "verify"]` and nothing stack-specific,
> per #16/#34); the configs below show how a real consumer *adapts* the
> composed gate to its own stack. Copy the shape, not the specifics.

The [composed verify gate](../architecture/foreman.md#the-runner-seam-v2) is a
**baseline plus capability-keyed additions**: Foreman runs `default` everywhere,
then each keyed command whose capability is present in the unit's runner.
Whatever can't run in-unit is GitHub Actions' job. The consumer's job is to map
its own `task` targets onto that shape.

## Example: a React / TypeScript web app

A port-binding e2e suite belongs behind the `ports` capability (present only
where a runner can bind ports without colliding — a lone sprite, not three
parallel local agents); the container-needing checks behind `docker`.

```toml
# .foreman.toml (consumer) — EXAMPLE
runner = "local"
trusted_actors = ["you", "your-bot"]

[verify]
default = ["task", "verify"]          # tsc + lint + unit tests (port-free)
docker  = ["task", "verify:docker"]   # e.g. testcontainers-backed integration
ports   = ["task", "e2e"]             # Playwright against a dev server
```

The consumer's Taskfile supplies those targets, following the one vocabulary
([conventions](../conventions.md#task-runner-taskfile)):

```yaml
# Taskfile.yml (consumer) — EXAMPLE, illustrative only
tasks:
  check:  { desc: "lint + format + typecheck (the fast hook gate)" }
  build:  { desc: "the application bundle" }
  test:   { desc: "port-free unit tests" }
  verify: { deps: [check, build, test] }      # = default gate above
  e2e:    { desc: "Playwright (binds a port)" }  # = the ports addition
  ci:     { deps: [verify, e2e], cmds: ["task security"] }
```

Under `runner = local` with `max_parallel = 3`, Foreman runs `default` (and
`docker` if a daemon is reachable), **skips** `ports`, and GitHub Actions runs
the e2e suite. Under `sprite` (v2.1) the unit advertises `ports`, so `e2e`
runs in-unit and Actions need not repeat it.

## Example: a Python library (no ports, no docker)

Most repos need neither capability — three lines and done:

```toml
# .foreman.toml (consumer) — EXAMPLE
runner = "local"
trusted_actors = ["you", "your-bot"]

[verify]
default = ["task", "verify"]   # ruff + mypy + pytest
```

## Example: a hard requirement

If the baseline gate *cannot run* without a capability (e.g. the unit tests
themselves need a Docker daemon via testcontainers), declare it hard — a
mismatch is refused at plan time, not silently skipped:

```toml
# .foreman.toml (consumer) — EXAMPLE
required_capabilities = ["docker"]     # planning refuses a runner without it

[verify]
default = ["task", "verify"]
```

Foreman's own repo is a live example of the generic baseline plus a
`docker`-keyed addition — see [`.foreman.toml`](../../.foreman.toml).
