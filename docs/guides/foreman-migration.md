# Migrating a consumer off vendored Foreman

Foreman v2 is distributed as a pinned `uvx` invocation from a git tag
([ADR 0003](../decisions/0003-foreman-v2-runner-seam.md) D11), not vendored
source. This guide moves a repository that still has a real
`scripts/foreman/` directory onto the packaged dependency. It is idempotent
and safe to run twice.

## Why this needs a deliberate step (#38)

`copier update` flips harmon-init's `use_foreman` from *copy source* to
*install a pinned dependency*, but **`copier update` does not delete files the
template stopped emitting**. A repo scaffolded before the flip ends up with
both a pinned dependency *and* stale vendored source, and `task foreman:*`
could resolve to either depending on path order — the worst outcome, because
it fails silently and inconsistently. So the vendored directory must be
removed explicitly, and you must be able to prove which Foreman runs.

## The migration

1. **Update the template** so the wrapper Taskfile invokes the console
   script, not vendored source:

   ```bash
   copier update
   ```

   Afterwards `taskfiles/foreman.yml` should read (copier owns this file, so
   the pin bumps here without merge conflicts):

   ```yaml
   vars:
     # renovate: datasource=github-tags depName=ponderousdev/foreman
     FOREMAN_VERSION: 2.0.0
     FOREMAN: uvx --from git+https://github.com/ponderousdev/foreman@v{{.FOREMAN_VERSION}} foreman
   tasks:
     plan: { cmds: ["{{.FOREMAN}} plan {{.CLI_ARGS}}"] }
     # ...
   ```

   The invocation changes from `PYTHONPATH=scripts python3 -m foreman` to the
   `foreman` console script.

2. **Remove the vendored source.** `copier update` left it behind:

   ```bash
   git rm -r scripts/foreman
   ```

   If your `.foreman.toml`, prompt overrides, or agent files under `.claude/`
   were repository-specific customizations, keep them — only the package
   source (`scripts/foreman/*.py`, `backends/`, `prompts/`, `signatures.toml`)
   is replaced by the dependency.

3. **Prove which Foreman runs.** After migration, the path must resolve to the
   pinned dependency and nothing else:

   ```bash
   # Must succeed with no scripts/foreman present. Foreman is invoked via
   # `uvx` (not PATH-installed), so assert the wrapper resolves to the pin
   # rather than looking for a `foreman` binary on PATH.
   task foreman:plan -- --issue <n>
   grep -q 'uvx --from git+https://github.com/ponderousdev/foreman@v' taskfiles/foreman.yml
   ```

   A quick guard for CI or a runbook: fail if vendored source reappears.

   ```bash
   test ! -d scripts/foreman || { echo "vendored foreman source is back"; exit 1; }
   ```

## Config migration (v1 → v2 keys)

The extraction renamed and restructured several `.foreman.toml` keys. Foreman
loads the old keys with a deprecation warning where it safely can, but migrate
them:

| v1 key | v2 replacement |
|---|---|
| `verify_command = ["task", "ci"]` | `[verify]` table — `default = ["task", "verify"]` plus capability-keyed additions (`docker`, `ports`). Legacy `verify_command` still loads as the `default` baseline, with a warning. |
| `comment_trust = ["OWNER", …]` | `trusted_actors = ["you", "your-bot", …]` — an explicit login list, not author-association tiers. The old key is ignored with a warning. |
| *(new)* `runner = "local"` | where units execute: `local` (v2.0), `sprite` (v2.1), `docker` (v2.2). |
| *(new)* `required_capabilities = []` | hard requirements; a mismatch is refused at plan time. |

The command that used to be `foreman preflight` (read-only issue analysis that
drafts correction comments) is now **`foreman vet`**. The `preflight` name now
belongs to the empirical security-assertion gate.

## Prerequisites the operator owns

- The consumer's Renovate must be able to read a **private** repo's tags to
  propose `FOREMAN_VERSION` bumps (install/grant the Renovate app on
  `ponderousdev/foreman`, or a `hostRules` token) — and the regex manager
  needs `extractVersion: ^v(?<version>.*)$` for the `v`-prefixed tags. Verify
  the first bump end-to-end; this failure mode is silent.
- The bot PAT's selected-repo list must include `ponderousdev/foreman`, or
  `uvx` cannot fetch it (D11). See
  [`../architecture/security.md`](../architecture/security.md).
