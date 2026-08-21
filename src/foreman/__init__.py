"""Foreman — deterministic supervisor for milestone-driven agent dispatch.

Reads a milestone's (or single issue's) dependency graph from GitHub,
dispatches ready units to isolated headless agents in git worktrees, verifies
their output with the repo's own CI gate, opens PRs, and shepherds those PRs
to mergeable. Every merge is a human decision — foreman never merges.

State of record is GitHub + git, re-derived every tick; foreman stores no
local state files (worktrees and logs are disposable operational artifacts).

Spec: specs/foreman-v2.md (this repo)
Docs: docs/architecture/foreman.md
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("foreman")
except PackageNotFoundError:  # running from a bare checkout without install
    __version__ = "2.6.0"
