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
- `claude-plan` / `claude-implement` / `claude-review` — `@claude …` on issues and PRs.
- `devcontainer-build.yml` — prebuilds the devcontainer images to GHCR on `.devcontainer/**` changes.
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
skopeo/OCI-tarball path. The step appends `IMAGE:sha-<sha>@sha256:<digest>` to its
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
