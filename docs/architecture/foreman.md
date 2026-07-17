# Foreman

Foreman is a **deterministic supervisor** for milestone-driven agent work. It
reads a milestone's (or a single issue's) dependency graph from GitHub,
dispatches the currently-unblocked issues to isolated headless agents through
a swappable **Runner seam**, verifies each result with the repo's own
composed gate, opens PRs, and keeps those PRs healthy (CI repair, review
adjudication, rebases) until a human merges them.

> **v2 (the Runner seam).** Where a unit executes is a config flip behind one
> seam: `local` (v2.0, subprocess in the bot devcontainer), `sprite` (v2.1,
> one Fly microVM per unit), `docker` (v2.2, one sibling container per unit).
> The immutable decisions behind this — the trust model, capabilities, the
> composed gate, distribution — are
> [ADR 0003](../decisions/0003-foreman-v2-runner-seam.md) (D1–D14); the living
> requirements are [`specs/foreman-v2.md`](../../specs/foreman-v2.md). The
> package lives at `src/foreman/` and installs as the `foreman` console script
> (there is no more `scripts/foreman/` vendored source — see
> [distribution](#distribution-d11) below).

Everything that can be a crisp pass/fail check is plain code; LLMs write code
and adjudicate review findings, but they never judge "done" and never gate
progression. Zero tokens are spent on coordination.

## Non-negotiables

- **No AI ever merges to main. Ever.** No auto-merge, no enabling flag. The
  human merge is the only mechanism that advances the dependency graph;
  foreman's job ends at "this PR is verified, adjudicated, and mergeable —
  here is the suggested merge order." Server-side enforcement (branch
  ruleset, code-owner review, bot token without bypass) is the boundary;
  prompts are only a mitigation.
- **Stateless in the repo.** Human inputs are stored (issue bodies, labels /
  issue fields, comments); machine state is re-derived every tick from GitHub
  - git and never stored. A stored status can lie after a crash; re-derived
  state cannot. Worktrees and `.foreman/` logs are disposable operational
  artifacts, never state-of-record.
- **Foreman never edits human-authored content.** Issue titles/bodies,
  milestone descriptions, and human/other-bot comments are read-only.
  Corrections travel as comments (human-approved), never as edits.

## The wave model

Nodes are dispatch units (a parent issue; its sub-issues ride along as the
internal task list — one unit, one PR). Edges are `blocked-by` dependencies
(native GitHub dependencies primary; a `Depends-on: #n` body trailer is the
fallback — both present and disagreeing fails loud).

A **wave** is the set of open units whose dependencies are all satisfied.
Because a satisfied dependency means *merged into the default branch by a
human*, every agent branches off the default branch with complete,
human-approved context — no stacked PRs, no cross-branch coordination. The
human merge advances the graph; the next run (or watch tick) discovers the
newly-unblocked wave. During a merge freeze foreman idles by design: PRs stay
healthy, nothing new dispatches, and the log says it is waiting on merges.

## Doneness (deterministic, hardened)

A dependency is satisfied only when:

- **Foreman-managed** (a closing PR carries the `foreman:unit=#N` marker):
  the issue is closed **and** that PR is merged into the discovered default
  branch, authored by the configured bot login, from a `foreman/...` attempt
  branch. A marker PR that never merged fails loud.
- **External** (no marker PR): the issue is closed as *completed*. Closed as
  *not planned* blocks, with guidance — remove the edge, or apply the
  explicit human override (`foreman=satisfied` field / `foreman:satisfied`
  label). Every plan/status output prints *how* each dependency was
  satisfied.

## Inputs (humans write, foreman reads)

Arming is **explicit by default**: only issues carrying the `foreman` input
dispatch. The value names the backend (`claude`, `mock`) or `approved` (repo
default backend); `hold` always wins; `satisfied` / `external` adjust
dependency semantics. Per-repo config can relax to default-armed
(`require_approval = false`), where invoking dispatch/watch is the arming
act and `hold` excludes units.

- **Org repos**: the org-level issue custom fields `foreman`,
  `foreman-budget-usd`, `foreman-timeout-min` (issue fields are org-only).
- **Personal-account repos**: `foreman:*` labels (boolean inputs only;
  numeric overrides need fields).
- `inputs = "auto"` probes availability once per run and prints the chosen
  mode in every summary. In fields mode, a `foreman:*` label on the same
  issue fails loud — two drifting sources of truth is a bug, not a feature.

A unit must also satisfy the **spec contract**: an `## Acceptance Criteria`
section with items tagged `[CI]` (must map to named automated tests) or
`[HUMAN]` (surfaced, never attempted; the PR then `Refs` instead of `Closes`
the parent). The conventional-commit type comes from the native issue type
(mapped via `type_map`) or a `type:` label on personal repos — disagreement
fails loud.

## Commands

All entry points are Taskfile tasks; each takes `-- --milestone <n|title>` or
`-- --issue <n>`:

```bash
task foreman:plan      # dry run: graph, waves, ready set, validation
task foreman:vet      # read-only agent spec analysis; drafts correction comments
task foreman:preflight # empirically assert the security controls (login, PR rule,
                       #   read-only token, no workflow edit, tag immutability)
task foreman:dispatch  # dispatch ready units → verify → open PRs (idempotent)
task foreman:shepherd  # repair CI, adjudicate reviews, rebase, merge order
task foreman:watch     # loop plan→dispatch→shepherd (-- --interval 5m)
task foreman:status    # read-only snapshot + human-action queue
task foreman:retry     # re-dispatch after a human closed a PR (-- --unit N)
task foreman:cleanup   # prune worktrees/branches for closed units
```

## Per-unit flow

1. **Skip if in flight** — an existing attempt branch or open PR means the
   unit is taken (derived, no state file). Held/un-armed units are skipped.
2. **Isolate** — `git worktree add` under `.worktrees/foreman/`, branch
   `foreman/<type>/<n>-<slug>` off the discovered `<remote>/<default>`.
3. **Prompt** — assembled deterministically: fixed preamble
   (`src/foreman/prompts/implementer-preamble.md`), the full issue +
   sub-issue bodies, **trusted** comments only (author in `trusted_actors`,
   or foreman's own corrections — drive-by comments on public repos are an
   injection surface), and the `## Handoff` sections from merged dependency
   PRs. A capability-conditional preamble adds the no-port-binding rule
   where `ports` is absent (#24).
4. **Dispatch** — the backend adapter runs headless in the worktree with a
   timeout enforced by foreman. The session ref is captured from the FIRST
   stream event (killed agents emit no final event; resume depends on this).
5. **Result contract** — the agent must write `result.json` (outside the
   worktree): status, summary, handoff, human tasks, proposed title, and the
   AC→test mapping. Exit 0 without a valid contract counts as a crash.
   Ambiguity escalates via `BLOCKED.md` + a blocked result — never invented
   through.
6. **Verify** — foreman composes the gate from advertised capabilities
   (baseline `[verify] default`, then capability-keyed additions) and, under
   local, runs it itself in the worktree; under sprite it travels to the
   guest. The agent's self-report is never trusted. A verify failure names
   the full log path and is classified by `signatures.toml` before any
   resume, so an environmental failure is never handed back as a code bug.
7. **Freshness gate** — immediately before pushing: the issue is still open,
   still armed, dependencies still satisfied, the spec hash (bodies +
   trusted comments) unchanged since dispatch, and no PR appeared meanwhile.
   Drift means no push and a flagged unit.
8. **PR** — non-draft (review bots skip drafts), machine-readable marker,
   `Closes #N` (or `Refs` when human tasks remain), test evidence, Handoff,
   and human-only-tasks sections. On failure the worktree, session ref, and
   a generated resume-state are preserved; the issue stays open so
   dependents stay blocked.
9. **Status comment** — exactly one foreman-owned comment per unit, found by
   marker and edited in place. Display only; never read back for decisions.

## Shepherd

Deterministic triggers → bounded agent actions on open foreman PRs:

- **Red CI** → classify by the signature catalog
  (`src/foreman/signatures.toml`) first. `environment` failures get one
  empty-commit retry (the retrigger primitive — assume the bot token cannot
  re-run workflow jobs) and then the human queue; an agent must never "fix"
  infra by weakening code. `quota_wait` (the agent backend's own usage
  window) idles until reset. Mechanical failures resume the unit's agent
  with the failing excerpt.
- **Behind/conflicting after a sibling merge** → `git merge-tree` dry run
  enumerates conflicts; clean rebases are mechanical, conflicted ones go to
  the agent (rebase additively, regenerate generated artifacts via tooling,
  re-verify) — always rebase, never merge-main.
- **Unresolved review threads** → the agent adjudicates each finding: apply
  (commit + reply + resolve) or decline with technical reasoning (bots are
  sometimes wrong; deterministic facts beat speculation). Blanket-accepting
  is prohibited. Foreman re-checks disposition completeness afterwards.
- **Green + adjudicated + mergeable** → `ready-to-merge` label plus a
  dependency-aware suggested merge order. Foreman performs no merge action
  of any kind.

## Watch mode and unattended runs

`task foreman:watch` loops plan→dispatch→shepherd with a heartbeat line per
tick (`.foreman/watch.log`) — silence must look different from health. Every
tick is stateless and idempotent: kill it, reboot, resume exactly where
reality is. Stop conditions: milestone complete, `.foreman-stop` file,
aggregate budget, N consecutive failing ticks.

For multi-day runs prefer a host that won't idle-stop (Codespaces force-stops
at its idle timeout regardless of a background loop; a coder workspace or any
persistent container is the reliable substrate). Cron invoking
`foreman:dispatch` + `foreman:shepherd` on a schedule is equivalent to the
live loop — statelessness makes the runtime substrate interchangeable.

**Billing**: `billing = "subscription"` (default) inherits the container's
`CLAUDE_CODE_OAUTH_TOKEN`; USD budgets are inert (timeout/turns bind) and the
quota-wait signature turns usage-window exhaustion into planned pauses.
`billing = "api"` exports `FOREMAN_ANTHROPIC_API_KEY` **only inside the
adapter process** (the container-wide `ANTHROPIC_API_KEY` strip stays), and
USD budgets bind. Switching is a config flip plus one secret.

## Security model

- **Server-side boundaries** (hold regardless of model behavior): default
  branch ruleset requiring PRs, code-owner review, and green checks, with
  the bot excluded from bypass; a fine-grained bot token without `workflows`
  write (a push touching `.github/workflows/**` is rejected by GitHub); no
  org/admin scopes.
- **Identity assertion**: before its first write, foreman requires the gh
  identity to equal `expected_login` — a leaked-context or wrong-account run
  refuses to write.
- **Write contract**: every GitHub mutation lives in
  `src/foreman/github.py` and nowhere else. Foreman may create/push its
  own branches, open non-draft PRs, edit its own PRs and their
  foreman-namespace labels, upsert one marker-identified status comment per
  unit, resolve threads it dispositioned, post human-approved vet
  corrections, and ensure its label definitions. It must never merge, close
  or reopen issues, edit issue bodies/titles, touch human comments, or write
  fields/types/dependency edges — those operations do not exist in the
  module, and the test suite greps to keep them absent.
- **Prompt-injection surface**: only trusted-actor comments (author in
  `trusted_actors`, or foreman's own) enter prompts, and untrusted authorship
  anywhere in the surface classifies the unit `untrusted-input` (D13);
  review-bot findings and CI logs are framed as claims to adjudicate, not
  instructions; agents run with conservative permission modes outside the
  sandboxed bot devcontainer (`FOREMAN_SANDBOXED=1`
  relaxes inside it).

## Configuration (.foreman.toml)

```toml
runner = "local"              # where units execute: local | sprite | docker
backend = "claude"            # default adapter; per-issue input overrides
require_approval = true       # explicit arming (false = default-armed + holds)
inputs = "auto"               # auto | fields | labels
trusted_actors = ["you", "your-bot"]  # D4/D13 trust boundary; see below
required_capabilities = []    # hard requirements; a mismatch is refused at plan time
max_parallel = 3
branch_prefix = "foreman"
expected_login = "your-bot"   # identity assertion; "" skips
billing = "subscription"      # subscription | api
sandboxed = false             # FOREMAN_SANDBOXED=1 env inside the bot container

# The composed verify gate (#29): a baseline plus capability-keyed additions.
# The gate runs the baseline and every addition whose capability is present;
# whatever does not run in-unit is GitHub Actions' job.
[verify]
default = ["task", "verify"]          # runs everywhere — no special capability
docker  = ["task", "verify:docker"]   # additionally when `docker` is present
ports   = ["task", "e2e"]             # additionally when `ports` is present (sprite)

[budgets]
dispatch_usd = 20.0           # binds in api billing mode
shepherd_usd = 10.0

[timeouts]
dispatch_min = 90
shepherd_min = 30
```

## The Runner seam (v2)

One `Runner` protocol; `local`/`sprite`/`docker` implement it; selection is
config. The seam must not leak: graph, GitHub, and eligibility code contain no
runner-*name* branches (enforced by `tests/test_leak.py`) — those layers vary
only by consuming advertised **capabilities**.

**Capabilities are computed per environment, never per runner class** (D7):
`docker` is probed (is a daemon reachable?), `ports` is derived from
`max_parallel`, and `untrusted-input` follows from the boundary by policy.
LocalRunner advertises `{"docker"}` and nothing else in v2.0 — the
concurrency-1 `ports` cell is physically true but deliberately withheld (D9).

**Trust (D4/D13).** Arming authorizes; authorship classifies. The actor on
the most recent arming-label event must be a `trusted_actor`, always. The
issue/sub-issue author and post-arming editors classify the *input*: any
untrusted contribution injects the `untrusted-input` capability into the
unit's requirements, so the unit is refused under `local` (naming `sprite`)
and dispatchable under `sprite`. A repo is untrusted-input unless it is
private *and* every account with access is a trusted actor (public ⇒ always
untrusted; unenumerable ⇒ fail closed). Plan-affecting config — `runner`,
`trusted_actors`, `required_capabilities`, `[verify]` — is read from the
default branch of Foreman's own clone, never a dispatched branch.

**Crash safety.** A unit's handle (PID + process start-time) is serialized
under `.foreman/runs/`; a restarted Foreman re-derives state from GitHub and
git, takes a per-unit lock, probes liveness, and reattaches rather than
redispatching. Exit status is recorded by the spawn wrapper (atomic rename),
so it survives a restart; a dead process with no recorded status is reported
as abnormal, never guessed.

## `foreman preflight` — the security assertion gate

Fine-grained token permissions cannot be introspected, so `foreman preflight`
proves five controls empirically before any dispatch, each bounded to a
scratch ref it cleans up: the write-token login matches `expected_login`; the
default branch requires PRs; the read token cannot write; the write token
cannot edit workflows; and the write token cannot create, move, or delete
version tags (D14). The ruleset bypass-actor audit is a documented
operator-tier check, not a bot assertion. (v1's read-only issue-analysis
command that used this name is now `foreman vet`.)

## Distribution (D11)

Consumers are not Python projects, so there is no package index: a repo pins a
version in its copier-owned wrapper Taskfile and invokes through `uvx`:

```yaml
# taskfiles/foreman.yml — template output
vars:
  # renovate: datasource=github-tags depName=ponderousdev/foreman
  FOREMAN_VERSION: 2.0.0
  FOREMAN: uvx --from git+https://github.com/ponderousdev/foreman@v{{.FOREMAN_VERSION}} foreman
```

Version tags are immutable (D14) because a moved tag is code execution in
every consumer's next `uvx` resolution. See
[migrating consumers off vendored Foreman](../guides/foreman-migration.md).

## Extending

- **Backends**: `src/foreman/backends/<name>.sh` is the entire vendor
  surface (`run` / `resume <ref>` / `capabilities`). v1 ships `claude.sh` and
  `mock.sh` (hermetic seam proof). A new vendor is one small file, added when
  concretely needed.
- **Runners**: implement the `Runner` protocol (`src/foreman/runner/`) and
  pair it with a commit-handoff strategy in `foreman.runner.select`. Nothing
  in dispatch changes.
- **Signatures**: when an unmatched CI failure gets an LLM diagnosis, add its
  regex to `signatures.toml` — the LLM diagnoses once, code recognizes
  forever.
- **Agent definitions**: `.claude/agents/foreman-*` wrap the same one-sourced
  runbooks in `src/foreman/prompts/` for interactive use.
