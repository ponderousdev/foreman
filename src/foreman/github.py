"""All GitHub access — every read and every mutation lives here, nowhere else.

Write contract (docs/architecture/foreman.md): foreman MAY create/push its own
branches, open draft PRs, promote its OWN drafts after the readiness gate,
return its OWN PRs to draft when readiness is invalidated, edit its OWN PRs
and their foreman-namespace labels, post bounded exact-head reviewer requests
on its own PRs, create/edit the single marker-identified status comment per
unit, resolve review threads it dispositioned, post human-approved vet
correction comments, idempotently ensure its label definitions exist, and —
the one issue-side mutation (#169) — write the consumer claim contract at
dispatch: add/remove an EXISTING `claim:*` label the consumer already defined
(never mint one) and upsert its own marker-identified claim-record comment, so
a consumer's fail-closed claim gate sees the in-flight unit and its
event-driven reconciliation can release a stranded claim.

Foreman MUST NEVER: merge anything, close/reopen issues, edit issue
titles/bodies/milestones, edit or delete human or third-party comments,
delete an issue, write custom-field values / issue types / dependency edges,
or touch repo settings. Those operations are deliberately absent from this
module, and tests/test_write_contract.py greps this file to keep them absent —
the sole `DELETE` verb permitted is removal of a `claim:*` label association
(reversible, namespace-guarded), never a comment or an issue.

Reads use `gh` JSON output (gh >= 2.96 exposes blockedBy, issueType,
subIssues, parent, closedByPullRequestsReferences); review threads and
exact-head external-review evidence are GraphQL-only reads.
"""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib.parse import quote

from foreman.config import Config
from foreman.util import ForemanError, run

Runner = Callable[[list[str], str | None], tuple[int, str, str]]

STATUS_MARKER = "<!-- foreman:unit-status -->"
# The consumer claim contract (#169) rides its OWN marked comment, kept
# distinct from the status comment so the two never race for the one status
# body. The marker prefix identifies foreman's own claim record for upsert
# and release; claim.py owns the full marker + payload shape.
CLAIM_MARKER = "<!-- foreman:claim"
# The only issue-label namespace foreman may touch. The label must ALREADY
# exist in the repo (the caller checks `repo_labels()` first) — foreman
# adds/removes an existing consumer `claim:*` label, it never mints one.
CLAIM_LABEL_PREFIX = "claim:"
# upsert_status_comment sentinel: "caller doesn't know" is distinct from
# "caller proved absent" — only the former may trigger a second lookup.
_UNKNOWN_COMMENT = object()

# GitHub Actions' own App id — stable and public. Required checks bound to
# this integration are satisfiable only by Actions job observations.
_ACTIONS_APP_ID = 15368

# Labels Foreman idempotently ensures and is allowed to apply to its own PRs.
# Legacy names are read-only compatibility aliases: never ensure or write them.
DISPATCHED_LABEL = "foreman:dispatched"
READY_FOR_REVIEW_LABEL = "foreman:ready-for-review"
READY_HEAD_PREFIX = "<!-- foreman:ready-head:"
LEGACY_DISPATCHED_LABEL = "foreman-dispatched"
LEGACY_READY_FOR_REVIEW_LABEL = "ready-to-merge"
DISPATCHED_LABELS = (DISPATCHED_LABEL, LEGACY_DISPATCHED_LABEL)
READY_FOR_REVIEW_LABELS = (
    READY_FOR_REVIEW_LABEL,
    LEGACY_READY_FOR_REVIEW_LABEL,
)

FOREMAN_LABELS = {
    DISPATCHED_LABEL: ("1D76DB", "PR opened by Foreman for a dispatched unit"),
    READY_FOR_REVIEW_LABEL: ("5319E7", "Automation complete; ready for human review"),
}

ISSUE_FIELDS = ",".join(
    [
        "number",
        "title",
        "body",
        "state",
        "stateReason",
        "labels",
        "milestone",
        "url",
        "author",
        "issueType",
        "parent",
        "subIssues",
        "blockedBy",
        "closedByPullRequestsReferences",
    ]
)

PR_LIST_FIELDS = (
    "number,title,body,url,state,isDraft,headRefName,baseRefName,labels,author"
)

REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100) {
        totalCount
        nodes {
          id
          isResolved
          isOutdated
          path
          comments(first: 50) {
            totalCount
            nodes { author { login } body url }
          }
        }
      }
    }
  }
}
"""

REVIEWER_EVIDENCE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      comments(first: 100) {
        totalCount
        nodes {
          body
          createdAt
          author { login }
          reactions(first: 100) {
            totalCount
            nodes { content createdAt user { login } }
          }
        }
      }
      reviews(first: 100) {
        totalCount
        nodes {
          state
          submittedAt
          author { login }
          commit { oid }
          comments(first: 1) { totalCount }
        }
      }
    }
  }
}
"""

RESOLVE_THREAD_MUTATION = """
mutation($threadId: ID!) {
  resolveReviewThread(input: { threadId: $threadId }) {
    thread { id isResolved }
  }
}
"""

REPLY_THREAD_MUTATION = """
mutation($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(
    input: { pullRequestReviewThreadId: $threadId, body: $body }
  ) {
    comment { id }
  }
}
"""

CONTENT_EDITS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      userContentEdits(first: 100) {
        nodes { editedAt editor { login } }
      }
    }
  }
}
"""


def _subprocess_runner(argv: list[str], input_text: str | None) -> tuple[int, str, str]:
    proc = run(["gh", *argv], input_text=input_text, check=False)
    return proc.returncode, proc.stdout, proc.stderr


class Gh:
    """Thin `gh` transport; tests inject a fake runner here."""

    def __init__(self, runner: Runner | None = None):
        self._runner = runner or _subprocess_runner

    def call(
        self, args: list[str], *, input_text: str | None = None, check: bool = True
    ) -> str:
        rc, out, err = self._runner(args, input_text)
        if check and rc != 0:
            raise ForemanError(
                f"gh {' '.join(args)} failed ({rc}): {err.strip() or out.strip()}"
            )
        return out

    def ok(self, args: list[str]) -> bool:
        rc, _out, _err = self._runner(args, None)
        return rc == 0

    def json(self, args: list[str], *, input_text: str | None = None) -> Any:
        out = self.call(args, input_text=input_text)
        try:
            return json.loads(out) if out.strip() else None
        except json.JSONDecodeError as exc:
            raise ForemanError(f"gh {' '.join(args)}: unparseable JSON output") from exc


class GitHub:
    """Repository-scoped GitHub facade enforcing the write contract."""

    def __init__(self, gh: Gh, cfg: Config):
        self.gh = gh
        self.cfg = cfg
        self.read_only = False
        self._identity_ok = False
        self._cache: dict[str, Any] = {}
        self._issue_cache: dict[int, dict] = {}

    # ── facts ────────────────────────────────────────────────────────

    def repo(self) -> dict:
        if "repo" not in self._cache:
            self._cache["repo"] = self.gh.json(
                [
                    "repo",
                    "view",
                    "--json",
                    "nameWithOwner,owner,name,defaultBranchRef,visibility",
                ]
            )
        return self._cache["repo"]

    def repo_slug(self) -> str:
        return self.repo()["nameWithOwner"]

    def owner(self) -> str:
        return self.repo()["owner"]["login"]

    def default_branch(self) -> str:
        return self.repo()["defaultBranchRef"]["name"]

    def viewer(self) -> str:
        if "viewer" not in self._cache:
            self._cache["viewer"] = self.gh.call(
                ["api", "user", "--jq", ".login"]
            ).strip()
        return self._cache["viewer"]

    # ── issue reads ──────────────────────────────────────────────────

    def issue(self, number: int, *, fresh: bool = False) -> dict:
        if fresh or number not in self._issue_cache:
            self._issue_cache[number] = self.gh.json(
                ["issue", "view", str(number), "--json", ISSUE_FIELDS]
            )
        return self._issue_cache[number]

    def issue_comments(self, number: int) -> list[dict]:
        """Comments with stable ids + author_association (REST, paginated)."""
        out = self.gh.json(
            [
                "api",
                f"repos/{self.repo_slug()}/issues/{number}/comments",
                "--paginate",
                "--slurp",
            ]
        )
        comments: list[dict] = []
        for page in out or []:
            comments.extend(page)
        return comments

    def find_status_comment(self, issue_number: int) -> dict | None:
        """The one comment foreman itself authored AND marked, if any — a
        READ (no write contract implications). A marker in anyone else's
        comment is forged or quoted, never foreman's status comment."""
        me = self.viewer()
        for comment in self.issue_comments(issue_number):
            author = (comment.get("user") or {}).get("login", "")
            if STATUS_MARKER in (comment.get("body") or "") and author == me:
                return comment
        return None

    def repo_labels(self) -> list[str]:
        """Every label DEFINED in the repo (names only) — the read behind the
        never-mint rule (#169): foreman writes a `claim:*` label only when the
        consumer has already defined it."""
        rows = self.gh.json(["label", "list", "--limit", "500", "--json", "name"]) or []
        return [row["name"] for row in rows if row.get("name")]

    def find_claim_comment(self, issue_number: int) -> dict | None:
        """The one claim-record comment foreman itself authored AND marked, if
        any — a READ. Same forge-resistance as the status comment: a marker in
        anyone else's comment is quoted or forged, never foreman's record."""
        me = self.viewer()
        for comment in self.issue_comments(issue_number):
            author = (comment.get("user") or {}).get("login", "")
            if CLAIM_MARKER in (comment.get("body") or "") and author == me:
                return comment
        return None

    def collaborator_logins(self) -> list[str]:
        """Every account with repository access (affiliation=all: direct,
        outside, and via org or team) — the D4 predicate's enumeration.
        Raises on failure so callers fail closed."""
        out = self.gh.json(
            [
                "api",
                f"repos/{self.repo_slug()}/collaborators?affiliation=all&per_page=100",
                "--paginate",
                "--slurp",
            ]
        )
        logins: list[str] = []
        for page in out or []:
            for collaborator in page or []:
                login = (collaborator or {}).get("login")
                if login:
                    logins.append(login)
        return logins

    def _issue_timeline(self, number: int) -> list[dict]:
        """Raw timeline events — deliberately UNCACHED: arming and rename
        attribution are TOCTOU re-checked pre-spawn and pre-push, and watch
        ticks re-derive; a cached timeline would let an event landing after
        planning slip past those gates. Fresh every call, like every other
        trust input."""
        out = self.gh.json(
            [
                "api",
                f"repos/{self.repo_slug()}/issues/{number}/timeline?per_page=100",
                "--paginate",
                "--slurp",
            ]
        )
        events: list[dict] = []
        for page in out or []:
            events.extend(event for event in page or [] if event)
        return events

    def issue_label_events(self, number: int) -> list[dict]:
        """Chronological `labeled` events: {label, actor, created_at} — the
        D13 arming-actor attribution source."""
        events: list[dict] = []
        for event in self._issue_timeline(number):
            if event.get("event") != "labeled":
                continue
            events.append(
                {
                    "label": ((event.get("label") or {}).get("name")) or "",
                    "actor": ((event.get("actor") or {}).get("login")) or "",
                    "created_at": event.get("created_at") or "",
                }
            )
        return events

    def issue_rename_events(self, number: int) -> list[dict]:
        """Chronological `renamed` events: {actor, created_at} — titles
        render into prompts, so renames classify like body edits (D13)."""
        events: list[dict] = []
        for event in self._issue_timeline(number):
            if event.get("event") != "renamed":
                continue
            events.append(
                {
                    "actor": ((event.get("actor") or {}).get("login")) or "",
                    "created_at": event.get("created_at") or "",
                }
            )
        return events

    def issue_content_edits(self, number: int) -> list[dict]:
        """Body-edit history: {editor, edited_at} — D13's post-arming-editor
        classification source (GraphQL; REST does not expose edits)."""
        out = self.gh.json(
            [
                "api",
                "graphql",
                "-f",
                f"query={CONTENT_EDITS_QUERY}",
                "-F",
                f"owner={self.owner()}",
                "-F",
                f"name={self.repo()['name']}",
                "-F",
                f"number={number}",
            ]
        )
        try:
            nodes = out["data"]["repository"]["issue"]["userContentEdits"]["nodes"]
        except (KeyError, TypeError) as exc:
            # Fail closed (D13): an unreadable edit history must never look
            # like "no untrusted edits" and silently permit dispatch.
            raise ForemanError(
                f"content edits: unreadable GraphQL response for issue "
                f"#{number} — failing closed (cannot attest the edit history)"
            ) from exc
        if nodes is not None and len(nodes) >= 100:
            # The connection caps at 100; a fuller page means edits we cannot
            # see. Fail closed rather than classify on partial evidence.
            raise ForemanError(
                f"content edits: issue #{number} has >= 100 edits — the "
                "history exceeds one page and cannot be fully attested; "
                "failing closed (D13)"
            )
        edits: list[dict] = []
        for node in nodes or []:
            edits.append(
                {
                    "editor": ((node or {}).get("editor") or {}).get("login") or "",
                    "edited_at": (node or {}).get("editedAt") or "",
                }
            )
        return edits

    def milestones(self, state: str = "open") -> list[dict]:
        return (
            self.gh.json(
                [
                    "api",
                    f"repos/{self.repo_slug()}/milestones?state={state}&per_page=100",
                ]
            )
            or []
        )

    def resolve_milestone(self, ident: str) -> dict:
        """Accept a milestone number or exact title; return the milestone."""
        for ms in self.milestones(state="all"):
            if str(ms["number"]) == str(ident) or ms["title"] == ident:
                return ms
        raise ForemanError(f"milestone not found: {ident}")

    def milestone_issue_numbers(self, title: str) -> list[int]:
        rows = (
            self.gh.json(
                [
                    "issue",
                    "list",
                    "--milestone",
                    title,
                    "--state",
                    "all",
                    "--limit",
                    "500",
                    "--json",
                    "number",
                ]
            )
            or []
        )
        return [row["number"] for row in rows]

    # ── PR reads ─────────────────────────────────────────────────────

    def prs(
        self, *, label: str | None = None, head: str | None = None, state: str = "open"
    ) -> list[dict]:
        args = [
            "pr",
            "list",
            "--state",
            state,
            "--limit",
            "200",
            "--json",
            PR_LIST_FIELDS,
        ]
        if label:
            args += ["--label", label]
        if head:
            args += ["--head", head]
        return self.gh.json(args) or []

    def pr_view(self, number: int, fields: str) -> dict:
        return self.gh.json(["pr", "view", str(number), "--json", fields])

    def pr_status(self, number: int) -> dict:
        status = self.pr_view(
            number,
            "number,title,body,url,state,isDraft,mergedAt,author,labels,"
            "headRefName,headRefOid,baseRefName,mergeable,mergeStateStatus",
        )
        if "statusCheckRollup" not in status:
            # #89: the GraphQL rollup is structurally unreadable by
            # fine-grained PATs (no Checks permission exists for them), so
            # CI state is derived from the two sources the bot token CAN
            # read — Actions workflow runs and combined commit status —
            # synthesized into the rollup shape classify_checks consumes.
            # A read failure raises (fail closed): an unreadable rollup
            # must never read as green. Tests may pre-supply a rollup in
            # the pr-view fixture, which is honored untouched.
            status["statusCheckRollup"] = self._check_contexts(
                status.get("headRefOid") or "",
                status.get("baseRefName") or self.default_branch(),
            )
            if (status.get("mergeStateStatus") or "").upper() == "UNSTABLE":
                # UNSTABLE = some non-passing check that is not required.
                # A failing third-party Checks-API run is invisible to both
                # derived sources and earns no required-context sentinel, so
                # GitHub's own verdict is the only signal — surface it as a
                # pending row: never green (no ready-for-review), never red
                # (no fixer agent chasing a check the PAT cannot see).
                status["statusCheckRollup"].append(
                    {
                        "name": "non-required checks "
                        "(mergeStateStatus UNSTABLE; unobservable via PAT)",
                        "status": "PENDING",
                        "conclusion": "",
                        "detailsUrl": "",
                    }
                )
        return status

    # Conclusions classify_checks treats as non-red; any other COMPLETED
    # conclusion (stale, startup_failure, future values) normalizes to
    # FAILURE — an unknown outcome must never read as green.
    _GREEN_CONCLUSIONS = {"SUCCESS", "SKIPPED", "NEUTRAL", ""}
    _RED_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "ACTION_REQUIRED", "CANCELLED", "ERROR"}

    def _check_contexts(self, sha: str, base_branch: str) -> list[dict]:
        if not sha:
            raise ForemanError("check contexts: PR has no head SHA — failing closed")
        # The same workflow can run repeatedly on one SHA: dedupe by
        # (workflow name, triggering event) keeping the newest run —
        # a superseded failure must not hold the rollup red, while a
        # manual workflow_dispatch must not displace the pull_request
        # suite (they are distinct check identities).
        latest: dict[tuple[str, str], dict] = {}
        runs = self.gh.json(
            [
                "api",
                f"repos/{self.repo_slug()}/actions/runs?head_sha={sha}&per_page=100",
                "--paginate",
                "--slurp",
            ]
        )
        for page in runs or []:
            for wf_run in (page or {}).get("workflow_runs") or []:
                # workflow_id is the stable identity; two workflow FILES can
                # share a display name, and merging them would drop one.
                key = (
                    wf_run.get("workflow_id") or wf_run.get("name") or "workflow",
                    wf_run.get("event") or "",
                )
                current = latest.get(key)
                if current is not None and (current.get("id") or 0) >= (
                    wf_run.get("id") or 0
                ):
                    continue
                latest[key] = wf_run
        contexts: list[dict] = []
        for wf_run in latest.values():
            wf_status = (wf_run.get("status") or "").upper()
            if wf_status != "COMPLETED":
                # The run status is authoritative. While a run is queued or
                # re-running, filter=all still returns prior-attempt jobs
                # whose stale green would mask the rerun (and whose stale
                # red would bait a redundant fixer) — so a non-completed
                # run contributes exactly one pending row and no jobs.
                contexts.append(
                    {
                        "name": wf_run.get("name") or "workflow",
                        "status": wf_status,
                        "conclusion": "",
                        "detailsUrl": wf_run.get("html_url") or "",
                        "_source": "actions",
                    }
                )
                continue
            # Required-status-check contexts name Actions JOBS, not the
            # workflow display name ("verify"/"security" vs "Build &
            # Validate") — and classification belongs at job granularity.
            # filter=all: a partial "re-run failed jobs" reuses the run id
            # and the default (latest attempt) omits previously successful
            # jobs. Jobs group by name; the whole group at the newest
            # attempt survives, so same-named siblings WITHIN an attempt
            # are all retained while superseded attempts are replaced.
            jobs_pages = self.gh.json(
                [
                    "api",
                    f"repos/{self.repo_slug()}/actions/runs/"
                    f"{wf_run.get('id') or 0}/jobs?filter=all&per_page=100",
                    "--paginate",
                    "--slurp",
                ]
            )
            groups: dict[str, list[dict]] = {}
            for page in jobs_pages or []:
                for job in (page or {}).get("jobs") or []:
                    groups.setdefault(job.get("name") or "job", []).append(job)
            jobs: list[dict] = []
            for group in groups.values():
                top = max((j.get("run_attempt") or 0) for j in group)
                jobs.extend(j for j in group if (j.get("run_attempt") or 0) == top)
            run_conclusion = self._normalize_conclusion(
                "COMPLETED", (wf_run.get("conclusion") or "").upper()
            )
            if not jobs:
                contexts.append(
                    {
                        "name": wf_run.get("name") or "workflow",
                        "status": wf_status,
                        "conclusion": run_conclusion,
                        "detailsUrl": wf_run.get("html_url") or "",
                        "_source": "actions",
                    }
                )
                continue
            any_red = False
            for job in jobs:
                job_status = (job.get("status") or "").upper()
                conclusion = self._normalize_conclusion(
                    job_status, (job.get("conclusion") or "").upper()
                )
                if conclusion in self._RED_CONCLUSIONS:
                    any_red = True
                contexts.append(
                    {
                        "name": job.get("name") or "job",
                        "status": job_status,
                        "conclusion": conclusion,
                        "detailsUrl": job.get("html_url") or "",
                        "_source": "actions",
                    }
                )
            if run_conclusion in self._RED_CONCLUSIONS and not any_red:
                # Backstop: the run itself concluded red but every job we
                # retained reads green — some failing job fell outside the
                # attempt view (e.g. a same-named sibling whose twin was
                # individually re-run). The run verdict wins; never let
                # job filtering launder a red run into a green rollup.
                contexts.append(
                    {
                        "name": wf_run.get("name") or "workflow",
                        "status": wf_status,
                        "conclusion": run_conclusion,
                        "detailsUrl": wf_run.get("html_url") or "",
                        "_source": "actions",
                    }
                )
        combined = self.gh.json(
            [
                "api",
                f"repos/{self.repo_slug()}/commits/{sha}/status?per_page=100",
                "--paginate",
                "--slurp",
            ]
        )
        for page in combined or []:
            for st in (page or {}).get("statuses") or []:
                state = (st.get("state") or "").upper()
                contexts.append(
                    {
                        "name": st.get("context") or "status",
                        "status": "PENDING" if state == "PENDING" else "COMPLETED",
                        "conclusion": "" if state == "PENDING" else state,
                        "detailsUrl": st.get("target_url") or "",
                        "_source": "commit-status",
                    }
                )
        # Third-party Checks-API contexts are invisible to both sources
        # above, and a required context may be BOUND to an integration
        # (integration_id): a same-named legacy status or foreign check
        # must not satisfy it. Actions-bound requirements accept only
        # Actions job observations; other-integration requirements are
        # always unobservable; unbound requirements accept either source.
        # Anything unsatisfied becomes a PENDING sentinel — the rollup can
        # be red or pending on its account, never falsely green.
        job_names = {c["name"] for c in contexts if c.get("_source") == "actions"}
        status_names = {
            c["name"] for c in contexts if c.get("_source") == "commit-status"
        }
        for req in self._required_contexts(base_branch):
            name = req["context"]
            integration = req.get("integration_id")
            if integration is None:
                satisfied = name in job_names or name in status_names
            elif integration == _ACTIONS_APP_ID:
                satisfied = name in job_names
            else:
                satisfied = False
            if not satisfied:
                contexts.append(
                    {
                        "name": f"{name} (required; unobservable via PAT)",
                        "status": "PENDING",
                        "conclusion": "",
                        "detailsUrl": "",
                    }
                )
        for context in contexts:
            context.pop("_source", None)
        return contexts

    def _normalize_conclusion(self, status: str, conclusion: str) -> str:
        if (
            status == "COMPLETED"
            and conclusion not in self._GREEN_CONCLUSIONS
            and conclusion not in self._RED_CONCLUSIONS
        ):
            return "FAILURE"
        return conclusion

    def _required_contexts(self, branch: str) -> list[dict]:
        pages = self.gh.json(
            [
                "api",
                f"repos/{self.repo_slug()}/rules/branches/"
                f"{quote(branch, safe='')}?per_page=100",
                "--paginate",
                "--slurp",
            ]
        )
        contexts: list[dict] = []
        for page in pages or []:
            for rule in page or []:
                if (rule or {}).get("type") != "required_status_checks":
                    continue
                params = rule.get("parameters") or {}
                for check in params.get("required_status_checks") or []:
                    name = (check or {}).get("context")
                    if name:
                        contexts.append(
                            {
                                "context": name,
                                "integration_id": (check or {}).get("integration_id"),
                            }
                        )
        return contexts

    def review_threads(self, number: int) -> list[dict]:
        out = self.gh.json(
            [
                "api",
                "graphql",
                "-f",
                f"query={REVIEW_THREADS_QUERY}",
                "-F",
                f"owner={self.owner()}",
                "-F",
                f"name={self.repo()['name']}",
                "-F",
                f"number={number}",
            ]
        )
        try:
            connection = out["data"]["repository"]["pullRequest"]["reviewThreads"]
            nodes = connection.get("nodes")
            total = connection.get("totalCount")
        except (KeyError, TypeError, AttributeError) as exc:
            # Fail closed (#54): an unreadable response must never read as
            # "no unresolved threads" — that path ends in ready-for-review.
            raise ForemanError(
                f"review threads: unreadable GraphQL response for PR "
                f"#{number} — failing closed"
            ) from exc
        if not isinstance(nodes, list) or not isinstance(total, int):
            # Partial metadata (null nodes, missing totalCount) is not an
            # empty complete page — it is evidence we cannot attest (#54).
            raise ForemanError(
                f"review threads: partial GraphQL response for PR #{number} "
                "(nodes/totalCount unreadable) — failing closed"
            )
        if total > len(nodes):
            # The connection caps at 100; more threads than fetched means
            # unresolved threads we cannot see. Fail closed rather than
            # let disposition completeness pass on partial evidence (#54).
            raise ForemanError(
                f"review threads: PR #{number} has {total} threads but only "
                f"{len(nodes)} were fetched — failing closed"
            )
        return nodes

    def reviewer_evidence(self, number: int) -> dict:
        """Complete bounded evidence for the configured external reviewer."""
        out = self.gh.json(
            [
                "api",
                "graphql",
                "-f",
                f"query={REVIEWER_EVIDENCE_QUERY}",
                "-F",
                f"owner={self.owner()}",
                "-F",
                f"name={self.repo()['name']}",
                "-F",
                f"number={number}",
            ]
        )
        try:
            pr = out["data"]["repository"]["pullRequest"]
            comments = pr["comments"]
            reviews = pr["reviews"]
            comment_nodes = comments["nodes"]
            review_nodes = reviews["nodes"]
            comment_total = comments["totalCount"]
            review_total = reviews["totalCount"]
        except (KeyError, TypeError, AttributeError) as exc:
            raise ForemanError(
                f"reviewer evidence: unreadable GraphQL response for PR "
                f"#{number} — failing closed"
            ) from exc
        if (
            not isinstance(comment_nodes, list)
            or not isinstance(review_nodes, list)
            or not isinstance(comment_total, int)
            or not isinstance(review_total, int)
            or comment_total > len(comment_nodes)
            or review_total > len(review_nodes)
        ):
            raise ForemanError(
                f"reviewer evidence: partial GraphQL response for PR #{number} "
                "— failing closed"
            )
        for comment in comment_nodes:
            reactions = (comment or {}).get("reactions") or {}
            nodes = reactions.get("nodes")
            total = reactions.get("totalCount")
            if (
                not isinstance(nodes, list)
                or not isinstance(total, int)
                or total > len(nodes)
            ):
                raise ForemanError(
                    f"reviewer evidence: partial reaction response for PR "
                    f"#{number} — failing closed"
                )
        return {
            "viewer": self.viewer(),
            "comments": comment_nodes,
            "reviews": review_nodes,
        }

    def branch_exists_remote(self, branch: str) -> bool:
        return self.gh.ok(["api", f"repos/{self.repo_slug()}/branches/{branch}"])

    def run_log_failed(self, run_url: str) -> str:
        """Failed-step log excerpt for an Actions run URL (best effort)."""
        run_id = run_url.rstrip("/").split("/")[-1]
        if not run_id.isdigit():
            return ""
        rc, out, _err = self.gh._runner(["run", "view", run_id, "--log-failed"], None)
        return out[-20000:] if rc == 0 else ""

    # ── write guard ──────────────────────────────────────────────────

    def _assert_writable(self, action: str) -> None:
        if self.read_only:
            raise ForemanError(
                f"write contract: '{action}' attempted in read-only mode"
            )
        if not self._identity_ok:
            expected = self.cfg.expected_login
            if expected:
                actual = self.viewer()
                if actual != expected:
                    raise ForemanError(
                        f"identity assertion failed: gh is authenticated as '{actual}' "
                        f"but config expects '{expected}' — refusing to write"
                    )
            self._identity_ok = True

    # ── guarded writes (the ENTIRE mutation surface) ─────────────────

    def ensure_labels(self) -> None:
        self._assert_writable("ensure labels")
        for name, (color, desc) in FOREMAN_LABELS.items():
            self.gh.call(
                [
                    "label",
                    "create",
                    name,
                    "--color",
                    color,
                    "--description",
                    desc,
                    "--force",
                ]
            )

    def create_pr(
        self, *, title: str, body: str, head: str, base: str, labels: list[str]
    ) -> str:
        self._assert_writable("create PR")
        args = [
            "pr",
            "create",
            "--title",
            title,
            "--body-file",
            "-",
            "--head",
            head,
            "--base",
            base,
            "--draft",
        ]
        for label in labels:
            args += ["--label", label]
        out = self.gh.call(args, input_text=body)
        return out.strip().splitlines()[-1] if out.strip() else ""

    def _own_pr_guard(self, number: int, action: str) -> dict:
        pr = self.pr_view(number, "number,author,labels,body,headRefOid,isDraft")
        if pr["author"]["login"] != self.viewer():
            raise ForemanError(
                f"write contract: '{action}' on PR #{number} not authored by foreman"
            )
        return pr

    def promote_own_pr(
        self, number: int, *, expected_head_oid: str
    ) -> tuple[bool, bool]:
        """Promote an own draft only when its head still matches the gate.

        GitHub's ready mutation has no compare-and-swap parameter, so the own
        PR guard performs the last possible head read immediately before it.
        The caller performs a post-transition read to catch the remaining
        network-sized race window.
        """
        self._assert_writable("promote own PR")
        pr = self._own_pr_guard(number, "promote draft")
        actual_head = pr.get("headRefOid") or ""
        if not expected_head_oid or actual_head != expected_head_oid:
            return False, False
        transitioned = bool(pr.get("isDraft"))
        if transitioned:
            try:
                self.gh.call(["pr", "ready", str(number)])
            except Exception:
                # The mutation may have reached GitHub even when its response
                # was lost. The guarded read knows this call could transition,
                # so compensate here before propagating the indeterminate result.
                self.draft_own_pr(number)
                raise
        return True, transitioned

    def draft_own_pr(self, number: int) -> None:
        """Return an own PR to its draft workbench (a fail-closed mutation)."""
        self._assert_writable("return own PR to draft")
        pr = self._own_pr_guard(number, "return to draft")
        if not pr.get("isDraft"):
            self.gh.call(["pr", "ready", str(number), "--undo"])

    def record_ready_head_own_pr(self, number: int, *, expected_head_oid: str) -> bool:
        """Record readiness using the body read by the guarded mutation."""
        self._assert_writable("record own PR ready head")
        pr = self._own_pr_guard(number, "record ready head")
        if (pr.get("headRefOid") or "") != expected_head_oid:
            return False
        lines = [
            line
            for line in (pr.get("body") or "").splitlines()
            if not line.startswith(READY_HEAD_PREFIX)
        ]
        body = "\n".join([*lines, f"{READY_HEAD_PREFIX}{expected_head_oid} -->", ""])
        self.gh.call(["pr", "edit", str(number), "--body-file", "-"], input_text=body)
        return True

    def edit_own_pr_body(self, number: int, body: str) -> None:
        self._assert_writable("edit own PR body")
        self._own_pr_guard(number, "edit body")
        self.gh.call(["pr", "edit", str(number), "--body-file", "-"], input_text=body)

    def label_own_pr(
        self,
        number: int,
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> None:
        self._assert_writable("label own PR")
        self._own_pr_guard(number, "label")
        # Namespace guard covers REMOVALS too (#54): stripping a human's
        # label from a PR is as much an out-of-contract write as adding one.
        for name in [*(add or []), *(remove or [])]:
            if name not in FOREMAN_LABELS:
                raise ForemanError(
                    f"write contract: label '{name}' outside the foreman namespace"
                )
        args = ["pr", "edit", str(number)]
        for name in add or []:
            args += ["--add-label", name]
        for name in remove or []:
            args += ["--remove-label", name]
        if len(args) > 3:
            self.gh.call(args)

    def comment_own_pr(self, number: int, body: str) -> None:
        self._assert_writable("comment on own PR")
        self._own_pr_guard(number, "comment")
        self.gh.call(
            ["pr", "comment", str(number), "--body-file", "-"], input_text=body
        )

    def request_reviewer_own_pr(
        self, number: int, *, expected_head_oid: str, body: str
    ) -> bool:
        """Post one reviewer request only while the exact qualified head remains."""
        self._assert_writable("request external review on own PR")
        pr = self._own_pr_guard(number, "request external review")
        if not expected_head_oid or pr.get("headRefOid") != expected_head_oid:
            return False
        self.gh.call(
            ["pr", "comment", str(number), "--body-file", "-"], input_text=body
        )
        return True

    def upsert_status_comment(
        self,
        issue_number: int,
        body: str,
        *,
        comment_id: int | None | object = _UNKNOWN_COMMENT,
    ) -> None:
        """Create or edit-in-place the single foreman status comment per unit.
        `comment_id` lets a caller that already ran `find_status_comment`
        skip the second lookup — a transient failure on a duplicate read
        must not lose a write the first read already earned. An explicit
        None means the caller proved no comment exists (straight to POST);
        omitting it means unknown (look it up here)."""
        self._assert_writable("upsert status comment")
        if STATUS_MARKER not in body:
            raise ForemanError("status comment body must carry the foreman marker")
        # Only ever edit a comment foreman itself authored AND marked.
        if comment_id is _UNKNOWN_COMMENT:
            comment = self.find_status_comment(issue_number)
        elif comment_id is None:
            comment = None
        else:
            comment = {"id": comment_id}
        if comment is not None:
            self.gh.call(
                [
                    "api",
                    "--method",
                    "PATCH",
                    f"repos/{self.repo_slug()}/issues/comments/{comment['id']}",
                    "-F",
                    "body=@-",
                ],
                input_text=body,
            )
            return
        self.gh.call(
            [
                "api",
                "--method",
                "POST",
                f"repos/{self.repo_slug()}/issues/{issue_number}/comments",
                "-F",
                "body=@-",
            ],
            input_text=body,
        )

    # ── consumer claim contract (#169) ───────────────────────────────
    #
    # The single issue-side mutation surface. Both writes are namespace- and
    # marker-guarded and target ONLY the consumer `claim:*` vocabulary; the
    # never-mint rule lives one layer up (claim.py checks `repo_labels()`).

    def _assert_claim_label(self, label: str, action: str) -> None:
        if not label.startswith(CLAIM_LABEL_PREFIX):
            raise ForemanError(
                f"write contract: '{action}' label '{label}' is outside the "
                f"'{CLAIM_LABEL_PREFIX}' namespace"
            )

    def add_issue_claim_label(self, number: int, label: str) -> None:
        """Add an existing consumer `claim:*` label to a unit issue. Adding is
        idempotent server-side, so a re-dispatch re-acquiring is a no-op."""
        self._assert_writable("add claim label")
        self._assert_claim_label(label, "add claim label")
        self.gh.call(
            [
                "api",
                "--method",
                "POST",
                f"repos/{self.repo_slug()}/issues/{number}/labels",
                "-f",
                f"labels[]={label}",
            ]
        )

    def remove_issue_claim_label(self, number: int, label: str) -> None:
        """Remove a consumer `claim:*` label at a terminal state. The only
        DELETE foreman issues, and it is confined to a label association — a
        reversible, namespace-guarded write, never a comment or issue. A label
        already gone (consumer reconciliation won the race) is not an error."""
        self._assert_writable("remove claim label")
        self._assert_claim_label(label, "remove claim label")
        self.gh.call(
            [
                "api",
                "--method",
                "DELETE",
                f"repos/{self.repo_slug()}/issues/{number}/labels/"
                f"{quote(label, safe='')}",
            ],
            check=False,
        )

    def upsert_claim_comment(
        self,
        issue_number: int,
        body: str,
        *,
        comment_id: int | None | object = _UNKNOWN_COMMENT,
    ) -> None:
        """Create or edit-in-place foreman's single claim-record comment per
        unit. Marker-required (a claim record with no marker could never be
        found again to release), and — like the status comment — only ever
        edits a comment foreman itself authored AND marked."""
        self._assert_writable("upsert claim comment")
        if CLAIM_MARKER not in body:
            raise ForemanError("claim comment body must carry the foreman claim marker")
        if comment_id is _UNKNOWN_COMMENT:
            comment = self.find_claim_comment(issue_number)
        elif comment_id is None:
            comment = None
        else:
            comment = {"id": comment_id}
        if comment is not None:
            self.gh.call(
                [
                    "api",
                    "--method",
                    "PATCH",
                    f"repos/{self.repo_slug()}/issues/comments/{comment['id']}",
                    "-F",
                    "body=@-",
                ],
                input_text=body,
            )
            return
        self.gh.call(
            [
                "api",
                "--method",
                "POST",
                f"repos/{self.repo_slug()}/issues/{issue_number}/comments",
                "-F",
                "body=@-",
            ],
            input_text=body,
        )

    def post_vet_correction(
        self, issue_number: int, body: str, *, human_approved: bool
    ) -> None:
        """The ONLY general issue-comment write — gated on explicit human approval."""
        if not human_approved:
            raise ForemanError("write contract: vet corrections require human approval")
        self._assert_writable("post approved vet correction")
        self.gh.call(
            [
                "api",
                "--method",
                "POST",
                f"repos/{self.repo_slug()}/issues/{issue_number}/comments",
                "-F",
                "body=@-",
            ],
            input_text=body,
        )

    def resolve_review_thread(self, pr_number: int, thread_id: str) -> None:
        """The caller passes the PR the thread belongs to so the own-PR
        guard applies — foreman's identity must never mutate threads on a
        PR it did not author (a foreign PR can carry the foreman label and
        a forged unit marker). Thread↔PR pairing is the shepherd's job: it
        accepts only ids read from this PR's own review threads."""
        self._assert_writable("resolve dispositioned review thread")
        self._own_pr_guard(pr_number, "resolve review thread")
        self.gh.json(
            [
                "api",
                "graphql",
                "-f",
                f"query={RESOLVE_THREAD_MUTATION}",
                "-F",
                f"threadId={thread_id}",
            ]
        )

    def reply_review_thread(self, pr_number: int, thread_id: str, body: str) -> None:
        """Post the agent's recorded disposition note as the thread reply.
        Agents hold a read-only token (#13), so foreman performs this write
        from the adjudication sidecar — the agent only records (#46). Same
        own-PR guard rationale as resolve_review_thread."""
        self._assert_writable("reply to dispositioned review thread")
        self._own_pr_guard(pr_number, "reply to review thread")
        self.gh.json(
            [
                "api",
                "graphql",
                "-f",
                f"query={REPLY_THREAD_MUTATION}",
                "-F",
                f"threadId={thread_id}",
                "-f",
                f"body={body}",
            ]
        )
