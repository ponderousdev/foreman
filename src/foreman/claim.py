"""The consumer claim contract (#169).

At dispatch, foreman-core — never the adapter, which holds a read-only token —
writes a claim marker on the unit issue *where the consumer vocabulary exists*,
so a consumer's fail-closed `claim:*` gate sees the in-flight unit and refuses
a concurrent mention-triggered agent on the same issue. This does not replace
#82's event-record status comment; it adds the state marker beside it.

Two things must both hold before foreman writes anything (otherwise it skips
cleanly, logged and non-fatal — foreman never mints a vocabulary a repo has
not opted into):

1. The consumer maps foreman's backend to a claim *family* in an
   `agent-registry.json` at the repo root (the harness mapping).
2. The repo already DEFINES the resulting `claim:<family>` label.

The record itself is a foreman-authored, marker-identified comment carrying a
machine-readable JSON payload in the documented shape, so the consumer's
event-driven release (`claim-release.yml` / `release-claim.sh`) can parse and
reconcile a claim foreman strands on a crash. Release is the exact inverse,
derived from what was written (the family is read back out of the record's
marker, never re-derived), so a terminal state releases exactly what dispatch
wrote.

Statelessness (spec): nothing here is foreman state-of-record. The label and
the comment live on the issue (a consumer input surface); a crash strands them
and the consumer reconciles — that is the whole point of engaging #82's
Option-3 decision rather than relitigating it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from foreman.config import Config
from foreman.github import CLAIM_LABEL_PREFIX, GitHub
from foreman.graph import Unit
from foreman.util import utc_now_iso

REGISTRY_FILE = "agent-registry.json"

# One HTML-comment marker line identifies foreman's own claim record AND
# carries the family + state as parseable attributes; the JSON block below it
# is the full documented payload. A shell reconciler can grep either.
_MARKER_RE = re.compile(r"<!--\s*foreman:claim\s+([^>]*?)-->")
_ATTR_RE = re.compile(r"(\w+)=([^\s]+)")

# Registry sub-objects a harness entry may nest its family under, and the
# keys a family value may live at — kept tolerant because the registry is the
# consumer's file, authored to its own conventions, not foreman's.
_REGISTRY_CONTAINERS = ("harnesses", "agents", "backends", "harness")
_FAMILY_KEYS = ("claim_family", "family", "claim")


def _marker(family: str, state: str) -> str:
    return f"<!-- foreman:claim family={family} state={state} -->"


def _family_from_value(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in _FAMILY_KEYS:
            got = value.get(key)
            if isinstance(got, str) and got.strip():
                return got.strip()
    return None


def _lookup(mapping: dict, backend_name: str) -> str | None:
    """Resolve a family from one harness→family mapping. Match the backend
    name directly, then its claude-code harness family (the adapters hardwire a
    provider family, e.g. `claude-code-glm` → `glm`)."""
    if not isinstance(mapping, dict):
        return None
    candidates = [backend_name]
    if backend_name.startswith("claude-code-"):
        candidates.append(backend_name[len("claude-code-") :])
    if backend_name == "claude" or backend_name.startswith("claude-code-"):
        candidates.append("claude")
    for candidate in candidates:
        if candidate in mapping:
            family = _family_from_value(mapping[candidate])
            if family:
                return family
    return None


def resolve_family(root: Path, backend_name: str) -> str | None:
    """The claim family the consumer maps foreman's backend to, or None when
    the repo ships no registry, the registry is unreadable, or it carries no
    mapping for this backend. Never raises — a malformed consumer file must
    skip the claim cleanly, never fail a dispatch."""
    path = root / REGISTRY_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    family = _lookup(data, backend_name)
    if family:
        return family
    for container in _REGISTRY_CONTAINERS:
        family = _lookup(data.get(container, {}), backend_name)
        if family:
            return family
    return None


def label_for(family: str) -> str:
    return f"{CLAIM_LABEL_PREFIX}{family}"


def _resolve_writable_label(
    gh: GitHub, root: Path, backend_name: str
) -> tuple[str | None, str | None, str]:
    """(family, label, skip_reason). label is non-None only when the consumer
    vocabulary fully exists: a registry mapping AND a repo-defined label."""
    family = resolve_family(root, backend_name)
    if family is None:
        return (
            None,
            None,
            f"no {REGISTRY_FILE} claim family for backend '{backend_name}'",
        )
    label = label_for(family)
    if label not in gh.repo_labels():
        return family, None, f"repo does not define '{label}' (never minting one)"
    return family, label, ""


def _record_body(family: str, unit: Unit, backend_name: str, branch: str) -> str:
    """The documented claim-record shape: a marker line the reconciler greps
    for family + state, then a JSON payload it parses, then a human line."""
    payload = {
        "owner": "foreman",
        "claim": label_for(family),
        "family": family,
        "backend": backend_name,
        "unit": unit.number,
        "branch": branch,
        "state": "active",
        "acquired": utc_now_iso(),
    }
    return "\n".join(
        [
            _marker(family, "active"),
            "",
            f"🔒 **Claim held by Foreman** — `{label_for(family)}`",
            "",
            "A live Foreman-dispatched unit owns this issue. A mention-triggered "
            "agent must not start a colliding attempt while this claim is active; "
            "Foreman releases it at a terminal state, and the consumer's "
            "event-driven reconciliation releases it if a crash strands it.",
            "",
            "```json",
            json.dumps(payload, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )


def _released_body(prior_body: str, family: str) -> str:
    """Flip a prior record to released in place — keep the provenance, update
    the state the marker and payload advertise so a reconciler reads it as
    free. Best-effort JSON rewrite: an unparseable prior body still gets the
    marker + a released note so the state is never left reading active."""
    header = _marker(family, "released")
    note = f"🔓 **Claim released by Foreman** — `{label_for(family)}` — {utc_now_iso()}"
    match = re.search(r"```json\n(.*?)\n```", prior_body, re.DOTALL)
    if match:
        try:
            payload = json.loads(match.group(1))
            payload["state"] = "released"
            payload["released"] = utc_now_iso()
            block = (
                "```json\n" + json.dumps(payload, indent=2, sort_keys=True) + "\n```"
            )
            return "\n".join([header, "", note, "", block, ""])
        except json.JSONDecodeError:
            pass
    return "\n".join([header, "", note, ""])


def parse_record(body: str) -> dict | None:
    """The consumer-side view of a record, for round-trip tests and release:
    {family, state} from the marker (authoritative — a shell reconciler can
    read it without a JSON parser), merged with the JSON payload when present.
    None when the body carries no foreman claim marker."""
    marker = _MARKER_RE.search(body or "")
    if not marker:
        return None
    record: dict[str, Any] = dict(_ATTR_RE.findall(marker.group(1)))
    payload = re.search(r"```json\n(.*?)\n```", body or "", re.DOTALL)
    if payload:
        try:
            data = json.loads(payload.group(1))
            if isinstance(data, dict):
                for key, value in data.items():
                    record.setdefault(key, value)
        except json.JSONDecodeError:
            pass
    return record


def acquire(
    gh: GitHub,
    cfg: Config,
    root: Path,
    unit: Unit,
    *,
    backend_name: str,
    branch: str,
    log: Callable[[str], None],
) -> None:
    """Write the claim contract at dispatch when the consumer vocabulary
    exists; skip cleanly (logged, non-fatal) otherwise. Never fails a dispatch
    — a claim-write failure is logged, and the consumer's fail-closed gate plus
    foreman's own freshness gate remain as the collision backstops."""
    try:
        family, label, skip = _resolve_writable_label(gh, root, backend_name)
    except Exception as exc:  # noqa: BLE001 — a claim read must not fail dispatch
        log(f"#{unit.number}: claim skipped — vocabulary probe failed: {exc}")
        return
    if label is None:
        log(f"#{unit.number}: claim skipped — {skip}")
        return
    assert family is not None
    try:
        gh.add_issue_claim_label(unit.number, label)
        gh.upsert_claim_comment(
            unit.number, _record_body(family, unit, backend_name, branch)
        )
        log(f"#{unit.number}: claim acquired — {label}")
    except Exception as exc:  # noqa: BLE001 — claim is advisory, never fatal
        log(f"#{unit.number}: claim write failed (non-fatal): {exc}")


def release(gh: GitHub, unit: Unit, *, log: Callable[[str], None]) -> None:
    """Release exactly what dispatch wrote: read the family back out of the
    record foreman authored, remove that `claim:<family>` label, and flip the
    record to released. Idempotent and non-fatal — no record means nothing to
    release (or the consumer already reconciled)."""
    try:
        prior = gh.find_claim_comment(unit.number)
    except Exception as exc:  # noqa: BLE001 — release must not fail a run
        log(f"#{unit.number}: claim release skipped — record read failed: {exc}")
        return
    if prior is None:
        return
    record = parse_record(prior.get("body") or "")
    family = (record or {}).get("family")
    if not family:
        log(f"#{unit.number}: claim release skipped — record carries no family")
        return
    label = label_for(family)
    try:
        gh.remove_issue_claim_label(unit.number, label)
        gh.upsert_claim_comment(
            unit.number,
            _released_body(prior.get("body") or "", family),
            comment_id=prior.get("id"),
        )
        log(f"#{unit.number}: claim released — {label}")
    except Exception as exc:  # noqa: BLE001 — release is best-effort
        log(f"#{unit.number}: claim release failed (non-fatal): {exc}")
