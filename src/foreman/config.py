"""Load .foreman.toml (repo root) with defaults and FOREMAN_* env overrides.

Config is intent, not state: nothing here is written back by foreman.
Python 3.11+ (tomllib).

Plan-affecting configuration — runner, image, trusted_actors,
required_capabilities, [reviewer], [verify] — is read from Foreman's OWN
clone (the repo root it was invoked in, on the default branch), never from a
dispatched branch: an agent must not be able to edit its own trust or its own
gate (#14). Worktree copies of this file are never loaded.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from foreman import capabilities as capabilities_mod
from foreman.util import ForemanError, warn

CONFIG_FILE = ".foreman.toml"

# Runner names describe WHERE execution happens (ADR 0003); the registry in
# foreman.runner resolves them. sprite ships in v2.1, docker in v2.2 — both
# names are already valid config so plan-time refusals can name them.
KNOWN_RUNNERS = ("local", "sprite", "docker")

# Agent image pin: <registry>/<repo>[:<tag>]@sha256:<64 hex>. Registry-agnostic
# (Foreman ships to consumer repos), registry port allowed, digest REQUIRED,
# tag optional. Used with .fullmatch(); the shell twin lives in
# scripts/agent-image-pin.sh.
_HOST = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*(?::[0-9]{1,5})?"
# Docker's path-component grammar: a separator run is a single `.`/`_`,
# a doubled `__`, or one-or-more `-` (so `my--image` and `a__b` are legal).
_SEG = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_TAG = r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}"
IMAGE_PIN_RE = re.compile(
    rf"(?:{_HOST}/)?{_SEG}(?:/{_SEG})*(?::{_TAG})?@sha256:[0-9a-f]{{64}}"
)


@dataclass
class Config:
    backend: str = "claude"
    backend_version: str = ""  # expected CLI version prefix; "" = don't assert
    runner: str = "local"  # where units execute: local | sprite (v2.1) | docker (v2.2)
    # Digest-pinned agent image, "" = unset. Consumed by the sprite runner
    # (#30); LocalRunner ignores it (it boots no image). Plan-affecting: read
    # from the default branch of Foreman's own clone, never a dispatched one.
    image: str = ""
    require_approval: bool = True  # strict arming: only foreman-approved units dispatch
    inputs: str = "auto"  # auto | fields | labels
    # The composed verify gate (#29): baseline plus capability-keyed
    # additions, run in declaration order where the capability is present.
    # Whatever does not run in-unit is GitHub Actions' job.
    verify: dict[str, list[str]] = field(
        default_factory=lambda: {"default": ["task", "verify"]}
    )
    # Hard capability requirements (#28): the baseline gate cannot run
    # without these; a mismatch is refused before dispatch.
    required_capabilities: list[str] = field(default_factory=list)
    # Accounts (humans and apps, e.g. "coderabbitai[bot]") whose content and
    # arming events are trusted (D4/D13). Empty = nobody is trusted: a
    # private repo with collaborators is then untrusted-input (fail closed).
    trusted_actors: list[str] = field(default_factory=list)
    # Optional external-review gate. ``reviewer_login`` identifies the account
    # whose GitHub-native evidence counts; ``reviewer_request`` is the exact
    # provider-specific request text Foreman posts without interpreting it.
    reviewer_login: str = ""
    reviewer_request: str = ""
    reviewer_timeout_min: int = 10
    reviewer_max_attempts: int = 2
    max_parallel: int = 3
    dispatch_budget_usd: float = 20.0
    shepherd_budget_usd: float = 10.0
    dispatch_timeout_min: int = 90
    shepherd_timeout_min: int = 30
    max_turns: int = 0  # 0 = uncapped (budget/timeout bind instead)
    branch_prefix: str = "foreman"
    worktrees_dir: str = ".worktrees/foreman"
    runtime_dir: str = ".foreman"
    type_map: dict[str, str] = field(
        default_factory=lambda: {
            "Feature": "feat",
            "Bug": "fix",
            "Task": "chore",
            "Research": "chore",
        }
    )
    default_type: str = "feat"
    expected_login: str = ""  # identity assertion before any write; "" = skip
    billing: str = "subscription"  # subscription | api
    sandboxed: bool = False  # true only inside the egress-limited bot devcontainer
    permission_mode: str = (
        ""  # "" = derived: bypassPermissions if sandboxed else acceptEdits
    )
    allow_not_planned: bool = False  # count closed-as-not-planned external deps as done
    remote: str = ""  # "" = auto-discover

    def resolved_permission_mode(self) -> str:
        if self.permission_mode:
            return self.permission_mode
        return "bypassPermissions" if self.sandboxed else "acceptEdits"


_ENV_OVERRIDES: dict[str, tuple[str, Callable[[str], object]]] = {
    "FOREMAN_BACKEND": ("backend", str),
    "FOREMAN_RUNNER": ("runner", str),
    "FOREMAN_INPUTS": ("inputs", str),
    "FOREMAN_MAX_PARALLEL": ("max_parallel", int),
    "FOREMAN_BILLING": ("billing", str),
    "FOREMAN_PERMISSION_MODE": ("permission_mode", str),
    "FOREMAN_SANDBOXED": ("sandboxed", lambda v: v.lower() in ("1", "true", "yes")),
}

_TABLES = {
    "reviewer": {
        "login": "reviewer_login",
        "request": "reviewer_request",
        "timeout_min": "reviewer_timeout_min",
        "max_attempts": "reviewer_max_attempts",
    },
    "budgets": {
        "dispatch_usd": "dispatch_budget_usd",
        "shepherd_usd": "shepherd_budget_usd",
    },
    "timeouts": {
        "dispatch_min": "dispatch_timeout_min",
        "shepherd_min": "shepherd_timeout_min",
    },
}

# v1 keys that no longer exist. verify_command maps into the composed gate's
# baseline (with a warning); comment_trust is superseded by trusted_actors
# (author-association trust cannot express an explicit actor list).
_REMOVED_KEYS = {
    "verify_command": (
        "verify_command is superseded by the [verify] table (#29); using it "
        "as [verify] default for now — migrate to: [verify]\\ndefault = [...]"
    ),
    "comment_trust": (
        "comment_trust is superseded by trusted_actors (D13): comments are "
        "embedded only when their author is a trusted actor. Key ignored."
    ),
}


def image_digest(ref: str) -> str:
    """The `sha256:<hex>` token of a pinned ref; "" when the ref carries no digest."""
    _, sep, digest = ref.partition("@sha256:")
    return f"sha256:{digest}" if sep else ""


def load(root: Path) -> Config:
    cfg = Config()
    path = root / CONFIG_FILE
    if path.exists():
        with path.open("rb") as fh:
            try:
                data = tomllib.load(fh)
            except tomllib.TOMLDecodeError as exc:
                raise ForemanError(f"{CONFIG_FILE}: invalid TOML: {exc}") from exc
        _apply(cfg, data, path.name)
    for env_name, (attr, cast) in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_name)
        if raw:
            try:
                setattr(cfg, attr, cast(raw))
            except ValueError as exc:
                raise ForemanError(
                    f"{env_name}: invalid value {raw!r} ({exc})"
                ) from exc
    _validate(cfg)
    return cfg


def _apply(cfg: Config, data: dict, source: str) -> None:
    known = set(Config.__dataclass_fields__)
    if "verify_command" in data and "verify" in data:
        raise ForemanError(
            f"{source}: both verify_command (deprecated) and [verify] are "
            "set — keep only [verify]"
        )
    for key, value in data.items():
        if key in _TABLES:
            if not isinstance(value, dict):
                raise ForemanError(f"{source}: [{key}] must be a table")
            for sub, attr in _TABLES[key].items():
                if sub in value:
                    setattr(cfg, attr, value.pop(sub))
            for leftover in value:
                warn(f"{source}: unknown key [{key}].{leftover} ignored")
        elif key in _REMOVED_KEYS:
            warn(f"{source}: {_REMOVED_KEYS[key]}")
            if key == "verify_command":
                cfg.verify = {"default": value}
        elif key in known:
            current = getattr(cfg, key)
            if isinstance(current, bool) and not isinstance(value, bool):
                raise ForemanError(f"{source}: '{key}' must be a boolean")
            setattr(cfg, key, value)
        else:
            warn(f"{source}: unknown key '{key}' ignored")


def _validate_verify_table(cfg: Config) -> None:
    if not isinstance(cfg.verify, dict) or not cfg.verify:
        raise ForemanError("config: [verify] must be a table with a default entry")
    if "default" not in cfg.verify:
        raise ForemanError(
            "config: [verify] needs a default entry — the baseline gate every unit runs"
        )
    capabilities_mod.validate_names(
        (key for key in cfg.verify if key != "default"),
        where="config: [verify]",
    )
    for key, command in cfg.verify.items():
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) for part in command)
        ):
            raise ForemanError(
                f"config: [verify] {key} must be a non-empty list of strings"
            )


def _validate(cfg: Config) -> None:
    if cfg.runner not in KNOWN_RUNNERS:
        raise ForemanError(
            f"config: runner must be one of {'|'.join(KNOWN_RUNNERS)}, "
            f"got '{cfg.runner}'"
        )
    if not isinstance(cfg.image, str):
        raise ForemanError("config: image must be a string")
    if cfg.image and not IMAGE_PIN_RE.fullmatch(cfg.image):
        raise ForemanError(
            "config: image must be <registry>/<repo>[:<tag>]@sha256:<64 hex> — "
            f"pin by digest, got '{cfg.image}'"
        )
    if cfg.inputs not in ("auto", "fields", "labels"):
        raise ForemanError(
            f"config: inputs must be auto|fields|labels, got '{cfg.inputs}'"
        )
    if cfg.billing not in ("subscription", "api"):
        raise ForemanError(
            f"config: billing must be subscription|api, got '{cfg.billing}'"
        )
    _validate_verify_table(cfg)
    if not isinstance(cfg.required_capabilities, list) or not all(
        isinstance(name, str) for name in cfg.required_capabilities
    ):
        raise ForemanError("config: required_capabilities must be a list of strings")
    capabilities_mod.validate_names(
        cfg.required_capabilities, where="config: required_capabilities"
    )
    if not isinstance(cfg.trusted_actors, list) or not all(
        isinstance(login, str) and login for login in cfg.trusted_actors
    ):
        raise ForemanError("config: trusted_actors must be a list of logins")
    if not isinstance(cfg.reviewer_login, str):
        raise ForemanError("config: [reviewer].login must be a string")
    if not isinstance(cfg.reviewer_request, str):
        raise ForemanError("config: [reviewer].request must be a string")
    if cfg.reviewer_login:
        if not cfg.reviewer_login.strip():
            raise ForemanError("config: [reviewer].login must be a non-empty login")
        if cfg.reviewer_login != cfg.reviewer_login.strip():
            raise ForemanError(
                "config: [reviewer].login must not have surrounding whitespace"
            )
        if cfg.reviewer_login.startswith("@") or any(
            char.isspace() for char in cfg.reviewer_login
        ):
            raise ForemanError(
                "config: [reviewer].login is an account login, without @ or spaces"
            )
        if not cfg.reviewer_request.strip():
            raise ForemanError(
                "config: [reviewer].request is required when login is configured"
            )
        if "<!-- foreman:" in cfg.reviewer_request:
            raise ForemanError(
                "config: [reviewer].request must not contain Foreman markers"
            )
    elif cfg.reviewer_request:
        raise ForemanError(
            "config: [reviewer].login is required when request is configured"
        )
    for key, value in (
        ("timeout_min", cfg.reviewer_timeout_min),
        ("max_attempts", cfg.reviewer_max_attempts),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ForemanError(f"config: [reviewer].{key} must be an integer >= 1")
    if not isinstance(cfg.max_parallel, int) or isinstance(cfg.max_parallel, bool):
        raise ForemanError("config: max_parallel must be an integer")
    if cfg.max_parallel < 1:
        raise ForemanError("config: max_parallel must be >= 1")
    if "/" in cfg.branch_prefix or not cfg.branch_prefix:
        raise ForemanError("config: branch_prefix must be a single path segment")
