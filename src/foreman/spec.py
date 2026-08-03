"""Issue spec contract validation and deterministic prompt assembly.

The contract: a dispatchable unit has an `## Acceptance Criteria` section;
items tag themselves `[CI]` (machine-verifiable, must map to named tests) or
`[HUMAN]` (surfaced, never attempted, blocks `Closes` for the parent).

Prompt assembly is pure code: fixed preamble (prompts/*.md, token-substituted)
+ full issue/sub-issue bodies + trusted comments + injected handoff contracts.
Comments are embedded only from trusted author associations (or foreman's own
posted corrections) — issue threads on public repos are otherwise an
injection surface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from foreman.config import Config
from foreman.github import STATUS_MARKER, GitHub
from foreman.graph import Unit, foreman_prs_for_issue
from foreman.util import pr_is_merged, sha256_hex

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

_HEADING_RE = re.compile(r"^(#{2,6})\s*(?P<title>.+?)\s*$", re.M)
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(?P<text>.+)$")


@dataclass
class AcItem:
    text: str
    tag: str  # CI | HUMAN
    tagged: bool


@dataclass
class SpecInfo:
    ac_items: list[AcItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def human_items(self) -> list[str]:
        return [item.text for item in self.ac_items if item.tag == "HUMAN"]


def find_section(body: str, title: str) -> str | None:
    """Return the text under a `## Title` heading (any 2-6 level), else None."""
    matches = list(_HEADING_RE.finditer(body or ""))
    for index, match in enumerate(matches):
        if match.group("title").strip().lower() == title.lower():
            start = match.end()
            level = len(match.group(1))
            for nxt in matches[index + 1 :]:
                if len(nxt.group(1)) <= level:
                    return body[start : nxt.start()].strip()
            return body[start:].strip()
    return None


def parse_ac(body: str) -> list[AcItem] | None:
    section = find_section(body, "Acceptance Criteria")
    if section is None:
        return None
    items: list[AcItem] = []
    for line in section.splitlines():
        match = _BULLET_RE.match(line)
        if not match:
            continue
        text = match.group("text").strip()
        if "[HUMAN]" in text:
            items.append(AcItem(text, "HUMAN", True))
        elif "[CI]" in text:
            items.append(AcItem(text, "CI", True))
        else:
            items.append(AcItem(text, "CI", False))
    return items


def validate(unit: Unit) -> SpecInfo:
    info = SpecInfo()
    items = parse_ac(unit.body)
    if items is None:
        info.errors.append(
            f"#{unit.number}: no '## Acceptance Criteria' section — non-dispatchable"
        )
        return info
    if not items:
        info.errors.append(f"#{unit.number}: Acceptance Criteria section has no items")
        return info
    info.ac_items = items
    untagged = sum(1 for item in items if not item.tagged)
    if untagged:
        info.warnings.append(
            f"#{unit.number}: {untagged} acceptance criteria untagged — treated as [CI]"
        )
    return info


def human_only_tasks(unit: Unit, info: SpecInfo) -> list[str]:
    tasks = list(info.human_items)
    section = find_section(unit.body, "Human-only tasks")
    if section:
        for line in section.splitlines():
            match = _BULLET_RE.match(line)
            if match:
                tasks.append(match.group("text").strip())
    return tasks


def trusted_comments(gh: GitHub, cfg: Config, number: int) -> tuple[list[dict], int]:
    """Comments safe to embed in prompts + count of excluded ones.

    Trusted means the author is in `trusted_actors` (or foreman itself) —
    the explicit-actor list that D4/D13 use everywhere; author-association
    trust (v1's comment_trust) is gone. Excluded comments never reach a
    prompt, which is why they do not join the D13 classification either."""
    kept: list[dict] = []
    excluded = 0
    me = gh.viewer()
    for comment in gh.issue_comments(number):
        author = (comment.get("user") or {}).get("login", "")
        if author == me and STATUS_MARKER in (comment.get("body") or ""):
            # Foreman's OWN unit-status comment is display-only and never
            # read back (ADR 0002) — without this filter it would qualify
            # as viewer-authored and re-enter prompts as a "correction",
            # feeding prior agent-generated text back as specification.
            # Author-scoped deliberately: a marker in anyone else's comment
            # is forged or quoted, not a status comment (the same rule
            # upsert_status_comment enforces), and flows through the
            # ordinary trust check below.
            continue
        if author == me or author in cfg.trusted_actors:
            kept.append(comment)
        else:
            excluded += 1
    return kept, excluded


def spec_hash(unit: Unit, comments: list[dict]) -> str:
    """Stable hash of everything the prompt embeds from the spec — titles
    included: they render into prompts and are user-controlled free text,
    so title drift is spec drift."""
    parts = [unit.title, unit.body]
    parts += [
        f"{sub.get('title') or ''}\n\x00\n{sub.get('body') or ''}"
        for sub in unit.sub_issues
    ]
    parts += [
        comment.get("body") or ""
        for comment in sorted(comments, key=lambda c: c.get("id", 0))
    ]
    return sha256_hex("\n\x00\n".join(parts))


def load_prompt(name: str, tokens: dict[str, str]) -> str:
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    for key, value in tokens.items():
        text = text.replace(f"%%{key}%%", value)
    return text


def extract_handoff(pr_body: str) -> str | None:
    section = find_section(pr_body or "", "Handoff")
    if section is None:
        return None
    # PR bodies end with a `---` footer; a thematic break ends the section.
    section = re.split(r"^\s*---\s*$", section, maxsplit=1, flags=re.M)[0].strip()
    return section or None


def collect_handoffs(gh: GitHub, cfg: Config, unit: Unit) -> list[tuple[int, str]]:
    """Handoff sections from the merged foreman PRs of this unit's dependencies."""
    handoffs: list[tuple[int, str]] = []
    for dep in unit.blocked_by:
        for pr in foreman_prs_for_issue(gh, cfg, dep):
            if not pr_is_merged(pr):
                continue
            text = extract_handoff(pr.get("body") or "")
            if text:
                handoffs.append((dep, text))
    return handoffs


def assemble_dispatch_prompt(
    gh: GitHub,
    cfg: Config,
    unit: Unit,
    *,
    branch: str,
    default_branch: str,
    result_file: str,
    comments: list[dict],
    excluded_comments: int,
    handoffs: list[tuple[int, str]],
    verify_display: str,
    capabilities: set[str],
    withheld_handoffs: list[int] | None = None,
) -> str:
    tokens = {
        "UNIT_NUMBER": str(unit.number),
        "UNIT_TITLE": unit.title,
        "BRANCH": branch,
        "DEFAULT_BRANCH": default_branch,
        "VERIFY_COMMAND": verify_display,
        "RESULT_FILE": result_file,
        "COMMIT_TYPE": unit.inputs.commit_type if unit.inputs else cfg.default_type,
    }
    sections = [load_prompt("implementer-preamble", tokens)]
    if "ports" not in capabilities:
        # Capability-conditional, never a global prompt constant (#24): the
        # constraint models concurrency in a shared netns, not "local". A
        # sprite has ports and must keep its browser.
        sections.append(load_prompt("ports-constraint", tokens))

    sections.append(f"# Unit #{unit.number}: {unit.title}\n\n{unit.body.strip()}")
    for sub in unit.sub_issues:
        state_note = (
            "" if (sub.get("state") or "").upper() == "OPEN" else " (already closed)"
        )
        sections.append(
            f"## Sub-issue #{sub['number']}: {sub.get('title', '')}{state_note}\n\n"
            f"{(sub.get('body') or '').strip()}"
        )

    if comments:
        rendered = []
        for comment in comments:
            author = (comment.get("user") or {}).get("login", "unknown")
            rendered.append(
                f"### Comment by @{author}\n\n{(comment.get('body') or '').strip()}"
            )
        sections.append(
            "# Issue comments (human corrections and clarifications — these amend the "
            "spec above and MUST be honored)\n\n" + "\n\n".join(rendered)
        )
    if excluded_comments:
        sections.append(
            f"_Note: {excluded_comments} comment(s) from untrusted authors were withheld "
            "from this prompt._"
        )

    if handoffs:
        rendered = [f"### Handoff from #{dep}\n\n{text}" for dep, text in handoffs]
        sections.append(
            "# Handoff contracts from merged dependencies (already on "
            f"{default_branch} — build on these, do not reinvent them)\n\n"
            + "\n\n".join(rendered)
        )
    if withheld_handoffs:
        withheld_list = ", ".join(f"#{dep}" for dep in withheld_handoffs)
        sections.append(
            f"_Note: handoff(s) from {withheld_list} were withheld — their "
            "origin issues carry untrusted contributions and this runner "
            "lacks the untrusted-input boundary._"
        )

    return "\n\n---\n\n".join(sections) + "\n"
