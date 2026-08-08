"""Provider-neutral, exact-head external-review readiness evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from foreman.config import Config

REVIEW_REQUEST_PREFIX = "<!-- foreman:review-request "


@dataclass(frozen=True)
class ReviewerVerdict:
    state: str
    detail: str
    attempts: int = 0
    next_attempt: int | None = None

    @property
    def ready(self) -> bool:
        return self.state in ("disabled", "success")

    @property
    def can_request(self) -> bool:
        return self.next_attempt is not None


def request_marker(head: str, attempt: int) -> str:
    return f"{REVIEW_REQUEST_PREFIX}head={head} attempt={attempt} -->"


def request_body(cfg: Config, head: str, attempt: int) -> str:
    return f"{cfg.reviewer_request}\n\n{request_marker(head, attempt)}\n"


def _request_marker(body: str) -> tuple[str, int] | None:
    for line in body.splitlines():
        if not line.startswith(REVIEW_REQUEST_PREFIX) or not line.endswith(" -->"):
            continue
        values = {}
        for part in line[len(REVIEW_REQUEST_PREFIX) : -4].strip().split():
            if "=" in part:
                key, value = part.split("=", 1)
                values[key] = value
        try:
            head = values["head"]
            attempt = int(values["attempt"])
        except (KeyError, ValueError):
            return None
        if head and attempt >= 1:
            return head, attempt
    return None


def _time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def reviewer_verdict(
    cfg: Config,
    head: str,
    evidence: dict,
    *,
    now: datetime | None = None,
) -> ReviewerVerdict:
    """Classify one named reviewer's evidence for exactly ``head``.

    Success is GitHub-native: an APPROVED review committed against this head,
    or THUMBS_UP on Foreman's marker-bound request comment. A review containing
    inline comments, CHANGES_REQUESTED, or THUMBS_DOWN is a finding. Evidence
    before the latest request attempt is superseded by that attempt.
    """
    if not cfg.reviewer_login:
        return ReviewerVerdict("disabled", "external reviewer gate disabled")
    if not head:
        return ReviewerVerdict("indeterminate", "reviewer head is unreadable")

    comments = evidence.get("comments")
    reviews = evidence.get("reviews")
    viewer = evidence.get("viewer")
    if not isinstance(comments, list) or not isinstance(reviews, list) or not viewer:
        return ReviewerVerdict("indeterminate", "reviewer evidence is unreadable")

    current_requests: list[tuple[int, datetime, dict]] = []
    stale = False
    viewer_login = str(viewer).casefold()
    for comment in comments:
        marker = _request_marker((comment or {}).get("body") or "")
        if marker is None or (
            (((comment or {}).get("author") or {}).get("login") or "").casefold()
            != viewer_login
        ):
            continue
        marker_head, attempt = marker
        if marker_head != head:
            stale = True
            continue
        created = _time((comment or {}).get("createdAt") or "")
        if created is None:
            return ReviewerVerdict(
                "indeterminate", "reviewer request timestamp is unreadable"
            )
        current_requests.append((attempt, created, comment))

    latest_request = max(
        current_requests, key=lambda row: (row[0], row[1]), default=None
    )
    attempts = latest_request[0] if latest_request else 0
    cutoff = latest_request[1] if latest_request else None
    reviewer = cfg.reviewer_login.casefold()
    success = False
    findings = False

    for review in reviews:
        review = review or {}
        if (((review.get("author") or {}).get("login") or "").casefold()) != reviewer:
            continue
        review_head = (review.get("commit") or {}).get("oid") or ""
        if review_head != head:
            stale = stale or bool(review_head)
            continue
        state = (review.get("state") or "").upper()
        if state in ("DISMISSED", "PENDING"):
            continue
        submitted = _time(review.get("submittedAt") or "")
        if submitted is None:
            return ReviewerVerdict(
                "indeterminate", "reviewer submission timestamp is unreadable", attempts
            )
        if cutoff is not None and submitted < cutoff:
            continue
        comment_count = (review.get("comments") or {}).get("totalCount")
        if not isinstance(comment_count, int):
            return ReviewerVerdict(
                "indeterminate", "reviewer finding count is unreadable", attempts
            )
        success = success or state == "APPROVED"
        findings = findings or state == "CHANGES_REQUESTED" or comment_count > 0

    if latest_request:
        reactions = (latest_request[2].get("reactions") or {}).get("nodes")
        if not isinstance(reactions, list):
            return ReviewerVerdict(
                "indeterminate", "reviewer reactions are unreadable", attempts
            )
        for reaction in reactions:
            reaction = reaction or {}
            if (
                ((reaction.get("user") or {}).get("login") or "").casefold()
            ) != reviewer:
                continue
            content = (reaction.get("content") or "").upper()
            success = success or content == "THUMBS_UP"
            findings = findings or content == "THUMBS_DOWN"

    if success and findings:
        return ReviewerVerdict(
            "contradictory",
            "reviewer reported both success and findings for the current head",
            attempts,
        )
    if success:
        return ReviewerVerdict(
            "success", "reviewer accepted the exact current head", attempts
        )
    if findings:
        next_attempt = attempts + 1 if attempts < cfg.reviewer_max_attempts else None
        return ReviewerVerdict(
            "findings",
            "reviewer reported findings for the exact current head",
            attempts,
            next_attempt,
        )
    if latest_request is None:
        return ReviewerVerdict(
            "stale" if stale else "missing",
            "reviewer evidence is stale for the current head"
            if stale
            else "reviewer evidence is missing for the current head",
            next_attempt=1,
        )

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    elapsed = now - latest_request[1]
    if elapsed.total_seconds() < cfg.reviewer_timeout_min * 60:
        return ReviewerVerdict(
            "pending",
            f"reviewer attempt {attempts} is pending for the current head",
            attempts,
        )
    if attempts < cfg.reviewer_max_attempts:
        return ReviewerVerdict(
            "timed_out",
            f"reviewer attempt {attempts} timed out for the current head",
            attempts,
            attempts + 1,
        )
    return ReviewerVerdict(
        "timed_out",
        f"reviewer timed out after {attempts} attempt(s) for the current head",
        attempts,
    )
