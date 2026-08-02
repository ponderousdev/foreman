"""Trust: the repo predicate (D4) and per-unit input classification (D13).

D4 — a repo is untrusted-input unless everyone who can create or edit its
issues is a trusted actor: public means always untrusted (the world can file
issues); private means untrusted unless every account with access
(collaborators, affiliation=all) appears in `trusted_actors`; if the access
list cannot be enumerated, fail closed.

D13 — arming authorizes; authorship classifies. The actor on the most
recent arming-label event must be trusted, on every runner, always.
Author and post-arming-editor trust classify the unit's input: any untrusted
contribution injects `untrusted-input` into the unit's required
capabilities. Pinning stays absolute: a trusted post-arming edit refreshes
the pin (a trusted edit is itself a new attestation — dispatch assembles
from current content and re-validates); an untrusted post-arming edit breaks
the attestation and fails closed until a trusted actor re-arms.

This module never names a runner: it only classifies and injects the
requirement. Refusal messages naming compatible runners are composed by
foreman.capabilities (the selection layer) — that split is what keeps D4/D13
enforceable without a runner-name branch in eligibility.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from foreman import inputs as inputs_mod
from foreman.capabilities import UNTRUSTED_INPUT
from foreman.config import Config
from foreman.github import GitHub
from foreman.util import ForemanError

if TYPE_CHECKING:
    from foreman.graph import Unit

# foreman:* label values that do NOT arm (see foreman.inputs).
_NON_ARMING = {inputs_mod.HOLD, inputs_mod.SATISFIED, inputs_mod.EXTERNAL}


@dataclass(frozen=True)
class RepoTrust:
    untrusted: bool
    reason: str


@dataclass
class UnitTrust:
    """Per-unit input classification. `refusals` are fail-closed conditions
    (broken attestation, unattributable arming) — never dispatch past one."""

    untrusted_input: bool = False
    contributors: list[str] = field(default_factory=list)
    arming_actor: str | None = None
    arming_at: str | None = None
    refusals: list[str] = field(default_factory=list)


def repo_trust(gh: GitHub, cfg: Config) -> RepoTrust:
    """The D4 predicate. Evaluated at plan time and re-evaluated at
    dispatch; any drift fails closed."""
    visibility = (gh.repo().get("visibility") or "").upper()
    if visibility != "PRIVATE":
        return RepoTrust(
            True,
            f"repository is {visibility.lower() or 'of unknown visibility'} "
            "— the world can create or edit issues",
        )
    if not cfg.trusted_actors:
        return RepoTrust(
            True, "trusted_actors is empty — nobody is trusted (fail closed)"
        )
    try:
        logins = gh.collaborator_logins()
    except ForemanError as exc:
        return RepoTrust(
            True, f"cannot enumerate repository access — fail closed ({exc})"
        )
    outsiders = sorted(set(logins) - set(cfg.trusted_actors))
    if outsiders:
        return RepoTrust(
            True,
            "accounts with repository access beyond trusted_actors: "
            + ", ".join("@" + login for login in outsiders),
        )
    return RepoTrust(False, "private; every account with access is a trusted actor")


def required_for(
    cfg: Config, repo: RepoTrust, unit_trust: UnitTrust | None
) -> set[str]:
    """A unit's effective hard requirements: the repo's declared
    `required_capabilities` plus the D4/D13 injection."""
    required = set(cfg.required_capabilities)
    if repo.untrusted:
        required.add(UNTRUSTED_INPUT)
    if unit_trust is not None and unit_trust.untrusted_input:
        required.add(UNTRUSTED_INPUT)
    return required


def _is_trusted(gh: GitHub, cfg: Config, login: str | None) -> bool:
    # D13 trusts exactly the configured actor list — nothing more. The
    # self-authored exception (foreman's own posted content) belongs only to
    # correction comments (spec.trusted_comments), never to arming, issue
    # authorship, or edits, so the viewer is NOT trusted here.
    if not login:
        return False
    return login in cfg.trusted_actors


def classify_unit(gh: GitHub, cfg: Config, unit: "Unit", mode: str) -> UnitTrust:
    """D13 classification over the unit's prompt surface: the issue body's
    author and editors, and every sub-issue's author and editors. Comments
    do not appear here because only trusted-authored comments are embedded
    at all (spec.trusted_comments); excluded comments never reach a prompt
    and therefore never classify."""
    out = UnitTrust()

    def classify(login: str | None, what: str) -> None:
        if not _is_trusted(gh, cfg, login):
            out.untrusted_input = True
            out.contributors.append(f"{what} @{login or 'unknown'}")

    armed = bool(unit.inputs and unit.inputs.armed)
    if armed:
        _attribute_arming(gh, cfg, unit, mode, out)

    classify(unit.author, f"author of #{unit.number}")
    _classify_edits(gh, cfg, unit.number, out)
    _classify_renames(gh, cfg, unit.number, out)
    for sub in unit.sub_issues:
        sub_number = sub.get("number")
        classify(
            (sub.get("author") or {}).get("login"),
            f"author of sub-issue #{sub_number}",
        )
        if isinstance(sub_number, int):
            _classify_edits(gh, cfg, sub_number, out)
            _classify_renames(gh, cfg, sub_number, out)
    return out


def _attribute_arming(
    gh: GitHub, cfg: Config, unit: "Unit", mode: str, out: UnitTrust
) -> None:
    """The actor on the most recent arming event must be trusted — on every
    runner, always. Label mode is attributable via the issue timeline;
    fields mode is not (GitHub exposes no actor for issue-field changes), so
    an armed fields-mode unit fails closed with instructions."""
    if mode == "fields":
        out.refusals.append(
            f"#{unit.number}: arming cannot be attributed in fields mode — "
            "GitHub exposes no actor for issue-field changes, and D13 "
            'requires a trusted arming actor. Use inputs = "labels".'
        )
        return
    events = [
        event
        for event in gh.issue_label_events(unit.number)
        if event["label"].startswith(inputs_mod.LABEL_PREFIX)
        and event["label"][len(inputs_mod.LABEL_PREFIX) :] not in _NON_ARMING
    ]
    if not events:
        if cfg.require_approval:
            out.refusals.append(
                f"#{unit.number}: armed, but no arming-label event is "
                "attributable from the timeline — fail closed (D13)"
            )
        # require_approval = false with no arming label: the repo opted into
        # config-level default-arming; that config is read from the default
        # branch and is itself the (human-committed) attestation.
        return
    last = events[-1]  # the timeline is chronological
    out.arming_actor = last["actor"] or None
    out.arming_at = last["created_at"] or None
    if not _is_trusted(gh, cfg, last["actor"]):
        out.refusals.append(
            f"#{unit.number}: arming label applied by untrusted actor "
            f"@{last['actor'] or 'unknown'} — a trusted actor must re-arm (D13)"
        )


def _classify_edits(gh: GitHub, cfg: Config, number: int, out: UnitTrust) -> None:
    """Body edits classify the unit; an UNTRUSTED edit after the arming
    event breaks the attestation (fail closed until re-armed). Timestamps
    are ISO-8601 UTC from the API, so string comparison orders them."""
    for edit in gh.issue_content_edits(number):
        editor = edit.get("editor") or ""
        edited_at = edit.get("edited_at") or ""
        if _is_trusted(gh, cfg, editor):
            continue  # a trusted edit refreshes the pin (new attestation)
        out.untrusted_input = True
        out.contributors.append(f"editor of #{number} @{editor or 'unknown'}")
        if out.arming_at and edited_at > out.arming_at:
            out.refusals.append(
                f"#{number}: body edited by untrusted actor "
                f"@{editor or 'unknown'} after arming — the attestation is "
                "broken; fail closed until a trusted actor re-arms the "
                "edited content (D13)"
            )


def _classify_renames(gh: GitHub, cfg: Config, number: int, out: UnitTrust) -> None:
    """Title renames classify exactly like body edits: titles render into
    prompts and join the spec hash, and GitHub's userContentEdits stream
    covers bodies only — rename attribution comes from the issue timeline.
    An untrusted rename after arming breaks the attestation, same as an
    untrusted post-arming body edit."""
    for rename in gh.issue_rename_events(number):
        actor = rename.get("actor") or ""
        renamed_at = rename.get("created_at") or ""
        if _is_trusted(gh, cfg, actor):
            continue  # a trusted rename refreshes the pin (new attestation)
        out.untrusted_input = True
        out.contributors.append(f"renamer of #{number} @{actor or 'unknown'}")
        if out.arming_at and renamed_at > out.arming_at:
            out.refusals.append(
                f"#{number}: renamed by untrusted actor "
                f"@{actor or 'unknown'} after arming — the attestation is "
                "broken; fail closed until a trusted actor re-arms the "
                "renamed content (D13)"
            )


def classify_branch_origin(gh: GitHub, cfg: Config, unit_number: int) -> UnitTrust:
    """D13 classification of the unit behind an existing branch, for fix
    dispatches on that branch (#46). A fix unit inherits its branch's
    classification: red-CI log text, conflict lists, and the branch's own
    tree derive from content the origin unit's authors and editors control,
    so a fix agent needs the same untrusted-input boundary the original
    dispatch did. Classifies the same surface as classify_unit — author,
    edits, sub-issue authors and edits — without re-attributing arming
    (arming authorizes dispatch; it does not classify content)."""
    out = UnitTrust()

    def classify(login: str | None, what: str) -> None:
        if not _is_trusted(gh, cfg, login):
            out.untrusted_input = True
            out.contributors.append(f"{what} @{login or 'unknown'}")

    issue = gh.issue(unit_number)
    classify((issue.get("author") or {}).get("login"), f"author of #{unit_number}")
    _classify_edits(gh, cfg, unit_number, out)
    _classify_renames(gh, cfg, unit_number, out)
    for ref in issue.get("subIssues") or []:
        sub_number = ref.get("number")
        if not isinstance(sub_number, int):
            continue
        sub = gh.issue(sub_number)
        classify(
            (sub.get("author") or {}).get("login"),
            f"author of sub-issue #{sub_number}",
        )
        _classify_edits(gh, cfg, sub_number, out)
        _classify_renames(gh, cfg, sub_number, out)
    return out
