# 4. Extend #82's Option-3 with a consumer claim marker

Date: 2026-08-13

## Status

Accepted

## Context

Issue #82 evaluated a dispatch-time *state indicator* on the unit issue and
deliberately chose the **event-record status comment (Option 3)** over a
persisted marker. Its case against a marker was the **cleanup obligation**: a
marker foreman writes must be removed on every exit, including a crash, and
foreman is stateless by design (machine state is re-derived, never stored), so
a stranded marker would be a lie no tick could correct.

Two facts changed downstream since (#169, from evanharmon1/harmon-init's
issue-strategy overhaul; refs #139 and harmon-init ADR 0005):

1. **Consumer claim gates are now fail-closed.** Repos on the harmon-init
   conventions run a `claim:*` gate: their mention-triggered agent workflows
   refuse any target already carrying a `claim:*`/`agent:*` label. A
   foreman-dispatched issue carries none, so a concurrent `@claude implement`
   on the same issue starts cleanly and collides with the in-flight unit —
   foreman's freshness gate only catches the second PR *after* both have spent
   work.
2. **The cleanup objection is answered by the consumer.** Consumer repos ship
   an event-driven release (`claim-release.yml` reconciles stale claims on
   issue close and unmerged PR close, parsing a documented claim-record
   comment). A claim foreman strands on a crash is repaired by machinery that
   already exists downstream — the obligation #82 could not meet is now met by
   someone else.

This ADR **engages** #82's decision rather than relitigating it: Option 3 was
right *given the facts then*; the facts changed.

## Decision

At dispatch, foreman-core (never the adapter — read-only token) writes the
**consumer claim contract** on the unit issue, **beside** #82's event-record
comment — it adds the state marker, it does not replace it:

- Add `claim:<family-of-backend>`, where the family is resolved from the
  consumer's root `agent-registry.json` harness mapping. **Skip cleanly** (log,
  non-fatal) when the repo ships no registry mapping for the backend, or when
  the repo does not already define the `claim:<family>` label. Foreman **never
  mints** a claim vocabulary a repo has not opted into.
- Post a marker-identified claim-record comment carrying a documented JSON
  payload, so the consumer's `claim-release.yml` / `release-claim.sh` can parse
  and reconcile it.
- Release both — remove exactly the label written, flip the record to
  `released` — at terminal failure (`failed`/`stale`/`refused`) and after a
  setup failure. `pr-open` **holds** the claim (the PR carries the work until a
  human merges); the consumer's event-driven release reconciles merge/close and
  anything a crash strands.

The implementation lives in `src/foreman/claim.py`; the two issue-side
mutations (`add`/`remove_issue_claim_label`, `upsert_claim_comment`) are the
only issue-side writes in the write contract, namespace- and marker-guarded in
`src/foreman/github.py`.

## Consequences

- A colliding mention-triggered agent is refused by the consumer's own gate
  the moment foreman claims the issue — the collision window #82 left open is
  closed at the source, not merely caught late by the freshness gate.
- Foreman remains stateless: the label and record live on the issue (a consumer
  input surface), never in foreman's state-of-record. A stranded claim is a
  consumer-reconciled condition, not foreman truth.
- The write contract grows its **first** issue-side mutation and its **only**
  `DELETE` verb — confined to removing a `claim:*` label association, never a
  comment or an issue, and enforced by a grep in `tests/test_write_contract.py`.
- **Not chosen:** minting a `claim:*` family when the repo lacks one (would
  impose foreman's vocabulary on a non-participating repo); replacing #82's
  event comment (the two are complementary — provenance log vs. live state);
  making foreman the crash-cleanup authority (the consumer's event-driven
  release already owns reconciliation, which is precisely what makes this safe).

## The [HUMAN] acceptance criterion

Issue #169's third acceptance criterion asks for a recorded human decision on
whether to **extend or decline** #82's Option-3 rationale. This ADR is that
record: **extend**, on the two changed facts above.
