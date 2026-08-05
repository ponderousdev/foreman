"""Git worktree + branch lifecycle. Remote and default branch are discovered,
never hardcoded. One worktree per unit under cfg.worktrees_dir; branches are
namespaced `<prefix>/<type>/<n>-<slug>` so cleanup and doneness can identify
foreman's own branches deterministically.
"""

from __future__ import annotations

import re
from pathlib import Path

from foreman.config import Config
from foreman.graph import Unit
from foreman.util import ForemanError, run, slugify, warn


def remote(cfg: Config) -> str:
    if cfg.remote:
        return cfg.remote
    names = [line for line in run(["git", "remote"]).stdout.split() if line]
    if len(names) == 1:
        return names[0]
    if "origin" in names:
        warn(
            "multiple git remotes; using 'origin' (set `remote` in .foreman.toml to override)"
        )
        return "origin"
    raise ForemanError(
        f"cannot pick a remote from {names}; set `remote` in .foreman.toml"
    )


def fetch(remote_name: str) -> None:
    run(["git", "fetch", "--prune", remote_name])


def base_sha(remote_name: str, branch: str) -> str:
    return run(["git", "rev-parse", f"{remote_name}/{branch}"]).stdout.strip()


def branch_name(cfg: Config, unit: Unit) -> str:
    commit_type = unit.inputs.commit_type if unit.inputs else cfg.default_type
    return f"{cfg.branch_prefix}/{commit_type}/{unit.number}-{slugify(unit.title)}"


def attempt_branches(cfg: Config, remote_name: str, number: int) -> list[str]:
    """Local + remote branches that are attempts for this unit."""
    pattern = re.compile(rf"^{re.escape(cfg.branch_prefix)}/[^/]+/{number}-")
    found: set[str] = set()
    local = run(
        ["git", "for-each-ref", "--format=%(refname:short)", "refs/heads/"]
    ).stdout
    for name in local.split():
        if pattern.match(name):
            found.add(name)
    remote_refs = run(["git", "ls-remote", "--heads", remote_name]).stdout
    for line in remote_refs.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].startswith("refs/heads/"):
            name = parts[1][len("refs/heads/") :]
            if pattern.match(name):
                found.add(name)
    return sorted(found)


def next_attempt_branch(base_name: str, existing: list[str]) -> str:
    if base_name not in existing:
        return base_name
    attempt = 2
    while f"{base_name}-r{attempt}" in existing:
        attempt += 1
    return f"{base_name}-r{attempt}"


def attempt_number(base_name: str, branch: str) -> int:
    """The attempt N a branch name implies — bare = 1, `-rN` suffix = N.
    Inverse of `next_attempt_branch`; anchoring the suffix on `base_name`
    keeps a slug that itself ends in `-r2` from misparsing. Never stored."""
    if branch == base_name:
        return 1
    match = re.fullmatch(rf"{re.escape(base_name)}-r(\d+)", branch)
    return int(match.group(1)) if match else 1


def worktree_path(cfg: Config, root: Path, unit: Unit) -> Path:
    return root / cfg.worktrees_dir / f"{unit.number}-{slugify(unit.title)}"


def add(path: Path, branch: str, start_point: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "worktree", "add", "-b", branch, str(path), start_point])


def add_existing_branch(path: Path, branch: str) -> None:
    """Recreate a worktree for an existing branch (e.g. after a machine restart)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "worktree", "add", str(path), branch])


def remove(path: Path, *, force: bool = True) -> None:
    args = ["git", "worktree", "remove", str(path)]
    if force:
        args.insert(3, "--force")
    run(args, check=False)
    run(["git", "worktree", "prune"], check=False)


def delete_branch(cfg: Config, remote_name: str, branch: str) -> None:
    """Delete a branch foreman created — refuses anything outside its namespace."""
    if not branch.startswith(f"{cfg.branch_prefix}/"):
        raise ForemanError(f"refusing to delete non-foreman branch '{branch}'")
    run(["git", "branch", "-D", branch], check=False)
    run(["git", "push", remote_name, "--delete", branch], check=False)


# Unit-boundary git operations (is_clean, commits_ahead, push,
# merge_tree_conflicts, rebase_onto, empty_commit, count_retrigger_commits)
# used to live here as `git -C <path>` calls. They moved to
# foreman.gitops.UnitGit, which routes them through Runner.exec so a remote
# runner slots in at the same call sites (#20). The functions below operate on
# Foreman's OWN clone and remote (not a unit worktree), so they remain plain
# local git.
