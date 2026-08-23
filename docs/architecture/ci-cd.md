# CI/CD

How continuous integration and delivery are wired in Foreman. Every
job delegates to `task` targets, so local hooks, CI, and humans run identical
commands (the Taskfile is the single source of truth).

## Quality gate

The pipeline runs `check → build → validate → test → security` (see
[../conventions.md](../conventions.md)). `build.yml` runs these as parallel jobs
plus an aggregate **`verify`** job; branch protection requires `verify` +
`security` to pass before a PR can merge to `main`.

## Workflows

- `build.yml` — on push/PR to `main`: lint, security, then the aggregate **`verify`** job. Security always runs gitleaks + dependency audit + Semgrep CE SAST.
- `claude-plan` / `claude-implement` / `claude-review` — **mention-only**: an
  explicit `@claude` mention naming `plan`, `implement`, or `review` in a
  comment or review from a sender on the `claude_authorized_members` allowlist. There is no
  label trigger and no open/assign trigger; the retired `claude-plan`,
  `claude-implement`, and `claude-review` labels are gone, because a label or an
  assignment carries no actor the allowlist can check on every path. Each run
  applies `claim:claude` to the target once the sender gate passes and removes it
  in an `always()` cleanup step, which covers the failure, step-timeout and
  cancellation paths. It is not a guarantee: a release whose DELETE fails leaves
  the marker in place and turns the job **red** on purpose (a masked failure
  would be permanent, since the next run reads the surviving claim and refuses),
  and runner loss, a force-cancel, or the job cap firing can strand the label
  with no cleanup at all. A stranded `claim:claude` blocks further mentions on
  that target until someone removes it by hand.
- `snyk-scheduled.yml` — optional weekly Snyk Code + Open
  Source second-opinion scans. It has schedule/manual triggers only, consumes no
  PR checks, and is not part of branch protection. See
  [security.md](security.md) for quota guidance.
- `devcontainer-build.yml` — prebuilds the devcontainer images to GHCR on `.devcontainer/**` changes.
- `claim-release.yml` — on `issues closed` and on `pull_request closed`
  **unmerged**, releases the claim markers an agent session left on an issue
  (assignee, `claim:*` label — or a legacy `agent:*` one, both of which
  `release-claim.sh` accepts — and the `Claiming —` comment's supersede). It
  holds `issues: write` and parses attacker-writable comment bodies, so it
  always checks out the **default branch** and never a PR head. It only wires
  events to `release-claim.sh` in the vendored `track-work` skill, so it
  no-ops until you have run `task sync:skills`.
- `release.yml` — release-please maintains the rolling release PR.
- `project-automation.yml` — syncs the org Project board status from PR/CI events.

### Agent image tags and digest

Every publish pushes two tags: `latest` and `sha-<commit>`. Not semver —
release-please never rebuilds the image (the paths filter fires on
`.devcontainer/**`, not on a release), so a version tag on the image would lie
about which commit built it; image versions and repo versions are deliberately
decoupled.

GHCR **tags are mutable; the digest is the identity.** A post-push step therefore
resolves the manifest digest with `docker buildx imagetools inspect` and asserts
the manifest is exactly `linux/amd64` — the `devcontainers/ci` action exposes no
digest output (`runCmdOutput` is its only one), and its `platform:` input is
deliberately **not** set because at the pinned SHA it switches the action onto a
skopeo/OCI-tarball path. The assertion runs *after* `push: always`, so it
**detects** a non-amd64 publish (the job goes red, `latest` has already moved)
rather than preventing it; the remedy is to re-push from an amd64 runner. The
digest pin in `.foreman.toml` is unaffected either way. The step appends `IMAGE:sha-<sha>@sha256:<digest>` to its
own job's step summary. Each matrix leg is a **separate job** — `publish (ai)` and
`publish (dev)` — with its own summary, so there is no combined page to read: open
the `publish (ai)` job, whose image is `foreman-devcontainer`, the agent image
(D6). `publish (dev)` builds the human profile and is never a pin source.

The published, pinnable artifact is **only ever** produced by this job. Local
`devcontainer up` builds (`scripts/devcontainer-smoke.sh`, `task ci`) are dev
tooling, never a pin source.

Renovate does not manage this pin: the GHCR package is private, so Mend-hosted
Renovate cannot read its tags. Bumping is manual and documented in
[../guides/devcontainers.md](../guides/devcontainers.md). Pull auth for the
private package from a Fly Machine is #30's concern, not this workflow's.

## Authentication

CI workflows authenticate as the **`ponderousdev-ci` GitHub App** (short-lived
tokens minted at runtime), not a PAT — see [security.md](security.md).
Third-party actions are pinned by commit SHA and bumped by Renovate.

## Releases

release-please opens a rolling release PR from conventional commits; merging it
cuts the tag, GitHub release, and CHANGELOG. Nothing auto-releases on a normal
merge.

TODO: document deployment targets/environments here once they exist; the deploy
how-to lives at [../guides/deploying.md](../guides/deploying.md).

## Runners

Jobs use `runs-on: ${{ fromJSON(vars.CI_RUNS_ON || '"ubuntu-latest"') }}`,
so the `CI_RUNS_ON` repository variable can move CI to different runners without
a commit.

That convenience is also the risk: it is a runtime change with no diff and no
review. **Do not point a public repository at a persistent self-hosted runner.**
The generated workflows already refuse to check out fork-controlled code on the
trusted aggregate job, but that contract bounds one specific job — it does not
make a long-lived runner safe for untrusted contributions generally. A fork PR
that can execute anything on a persistent runner can read its filesystem, its
credentials, and whatever the previous job left behind.

Before setting `CI_RUNS_ON` to a self-hosted value, audit every workflow for
`pull_request_target` and for any step that runs code from the PR head. Keep
untrusted-contribution workflows on GitHub-hosted runners.
