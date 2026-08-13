"""The write contract, enforced: read-only mode blocks every mutation before
any gh call; identity assertion gates the first write; forbidden operations
(merge, issue close/edit/reopen) do not exist in the mutation surface at all.
"""

from __future__ import annotations

import inspect
import re
import unittest

from foreman import github as github_mod
from foreman.config import Config
from foreman.github import (
    CLAIM_MARKER,
    DISPATCHED_LABEL,
    FOREMAN_LABELS,
    READY_FOR_REVIEW_LABEL,
    STATUS_MARKER,
)
from foreman.util import ForemanError
from tests.fakes import make_github


def _mutations(gh):
    return [
        ("ensure_labels", lambda: gh.ensure_labels()),
        (
            "create_pr",
            lambda: gh.create_pr(title="t", body="b", head="h", base="main", labels=[]),
        ),
        (
            "promote_own_pr",
            lambda: gh.promote_own_pr(1, expected_head_oid="abc"),
        ),
        ("draft_own_pr", lambda: gh.draft_own_pr(1)),
        (
            "record_ready_head_own_pr",
            lambda: gh.record_ready_head_own_pr(1, expected_head_oid="abc"),
        ),
        ("edit_own_pr_body", lambda: gh.edit_own_pr_body(1, "b")),
        (
            "label_own_pr",
            lambda: gh.label_own_pr(1, add=[READY_FOR_REVIEW_LABEL]),
        ),
        ("comment_own_pr", lambda: gh.comment_own_pr(1, "b")),
        (
            "request_reviewer_own_pr",
            lambda: gh.request_reviewer_own_pr(
                1, expected_head_oid="abc", body="review"
            ),
        ),
        (
            "upsert_status_comment",
            lambda: gh.upsert_status_comment(1, STATUS_MARKER + "\nb"),
        ),
        (
            "add_issue_claim_label",
            lambda: gh.add_issue_claim_label(1, "claim:claude"),
        ),
        (
            "remove_issue_claim_label",
            lambda: gh.remove_issue_claim_label(1, "claim:claude"),
        ),
        (
            "upsert_claim_comment",
            lambda: gh.upsert_claim_comment(1, CLAIM_MARKER + " -->\nb"),
        ),
        (
            "post_vet_correction",
            lambda: gh.post_vet_correction(1, "b", human_approved=True),
        ),
        ("resolve_review_thread", lambda: gh.resolve_review_thread(1, "T_x")),
        ("reply_review_thread", lambda: gh.reply_review_thread(1, "T_x", "b")),
    ]


class ReadOnlyMode(unittest.TestCase):
    def test_every_mutation_refuses_in_read_only_mode(self):
        gh, runner = make_github()
        gh.read_only = True
        baseline = len(runner.calls)
        for name, call in _mutations(gh):
            with self.assertRaises(ForemanError, msg=name):
                call()
        # No gh process was ever invoked by a refused mutation.
        self.assertEqual(len(runner.calls), baseline)


class IdentityAssertion(unittest.TestCase):
    def test_wrong_identity_blocks_first_write(self):
        cfg = Config(expected_login="evanharmon1-bot")  # fake viewer is "bot"
        gh, runner = make_github(cfg)
        with self.assertRaises(ForemanError) as ctx:
            gh.ensure_labels()
        self.assertIn("identity assertion failed", str(ctx.exception))
        self.assertFalse(runner.called_with_prefix(["label"]))

    def test_matching_identity_allows_writes(self):
        cfg = Config(expected_login="bot")
        gh, runner = make_github(cfg)
        runner.when(["label", "create"], "")
        gh.ensure_labels()
        self.assertTrue(runner.called_with_prefix(["label", "create"]))


class LabelVocabulary(unittest.TestCase):
    def test_write_contract_contains_only_namespaced_current_labels(self):
        self.assertEqual(
            set(FOREMAN_LABELS), {DISPATCHED_LABEL, READY_FOR_REVIEW_LABEL}
        )
        self.assertTrue(all(name.startswith("foreman:") for name in FOREMAN_LABELS))

    def test_ready_description_claims_only_automation_complete_and_human_turn(self):
        description = FOREMAN_LABELS[READY_FOR_REVIEW_LABEL][1].lower()
        self.assertIn("automation complete", description)
        self.assertIn("human review", description)
        self.assertNotIn("merge", description)


class GuardedChannels(unittest.TestCase):
    def test_pr_creation_is_always_draft(self):
        gh, runner = make_github()
        runner.when(["pr", "create"], "https://github.com/owner/repo/pull/9")
        gh.create_pr(title="t", body="b", head="h", base="main", labels=[])
        creates = runner.called_with_prefix(["pr", "create"])
        self.assertEqual(len(creates), 1)
        self.assertIn("--draft", creates[0])

    def test_promotion_requires_the_exact_current_head(self):
        gh, runner = make_github()
        runner.when(
            ["pr", "view", "9"],
            {
                "number": 9,
                "author": {"login": "bot"},
                "labels": [],
                "body": "",
                "headRefOid": "new-head",
                "isDraft": True,
            },
        )
        self.assertEqual(
            gh.promote_own_pr(9, expected_head_oid="old-head"), (False, False)
        )
        self.assertFalse(runner.called_with_prefix(["pr", "ready"]))

    def test_matching_head_promotes_and_non_ready_returns_to_draft(self):
        gh, runner = make_github()
        runner.when(
            ["pr", "view", "9"],
            {
                "number": 9,
                "author": {"login": "bot"},
                "labels": [],
                "body": "",
                "headRefOid": "head",
                "isDraft": True,
            },
        )
        runner.when(["pr", "ready", "9"], "")
        self.assertEqual(gh.promote_own_pr(9, expected_head_oid="head"), (True, True))
        self.assertEqual(len(runner.called_with_prefix(["pr", "ready", "9"])), 1)

        gh2, runner2 = make_github()
        runner2.when(
            ["pr", "view", "9"],
            {
                "number": 9,
                "author": {"login": "bot"},
                "labels": [],
                "body": "",
                "headRefOid": "head",
                "isDraft": False,
            },
        )
        runner2.when(["pr", "ready", "9", "--undo"], "")
        gh2.draft_own_pr(9)
        self.assertEqual(
            runner2.called_with_prefix(["pr", "ready", "9", "--undo"]),
            [["pr", "ready", "9", "--undo"]],
        )

    def test_indeterminate_promotion_attempts_compensating_draft(self):
        gh, runner = make_github()
        runner.when(
            ["pr", "view", "9"],
            {
                "number": 9,
                "author": {"login": "bot"},
                "labels": [],
                "body": "",
                "headRefOid": "head",
                "isDraft": True,
            },
        )
        runner.when(["pr", "ready", "9"], "lost response", rc=1)

        with self.assertRaises(ForemanError):
            gh.promote_own_pr(9, expected_head_oid="head")

        self.assertEqual(len(runner.called_with_prefix(["pr", "view", "9"])), 2)

    def test_ready_head_marker_preserves_latest_body(self):
        gh, runner = make_github()
        runner.when(
            ["pr", "view", "9"],
            {
                "number": 9,
                "author": {"login": "bot"},
                "labels": [],
                "body": "maintainer update\n",
                "headRefOid": "head",
                "isDraft": False,
            },
        )
        runner.when(["pr", "edit", "9", "--body-file", "-"], "")

        self.assertTrue(gh.record_ready_head_own_pr(9, expected_head_oid="head"))
        edits = [
            body
            for argv, body in runner.calls
            if argv[:4] == ["pr", "edit", "9", "--body-file"]
        ]
        self.assertEqual(
            edits, ["maintainer update\n<!-- foreman:ready-head:head -->\n"]
        )

    def test_reviewer_request_requires_exact_current_head(self):
        gh, runner = make_github()
        runner.when(
            ["pr", "view", "9"],
            {
                "number": 9,
                "author": {"login": "bot"},
                "labels": [],
                "body": "",
                "headRefOid": "new-head",
                "isDraft": True,
            },
        )
        self.assertFalse(
            gh.request_reviewer_own_pr(
                9, expected_head_oid="old-head", body="@review-bot review"
            )
        )
        self.assertFalse(runner.called_with_prefix(["pr", "comment", "9"]))

        runner.when(["pr", "comment", "9"], "")
        self.assertTrue(
            gh.request_reviewer_own_pr(
                9, expected_head_oid="new-head", body="@review-bot review"
            )
        )
        self.assertEqual(len(runner.called_with_prefix(["pr", "comment", "9"])), 1)

    def test_vet_comment_requires_human_approval(self):
        gh, runner = make_github()
        with self.assertRaises(ForemanError):
            gh.post_vet_correction(1, "body", human_approved=False)
        self.assertFalse(runner.called_with_prefix(["api", "--method", "POST"]))

    def test_own_pr_guard_rejects_foreign_prs(self):
        gh, runner = make_github()
        runner.when(
            ["pr", "view", "9"],
            {"number": 9, "author": {"login": "human"}, "labels": [], "body": ""},
        )
        with self.assertRaises(ForemanError):
            gh.edit_own_pr_body(9, "new body")

    def test_thread_mutations_reject_foreign_prs(self):
        # A foreign PR can carry the foreman label and a forged unit marker;
        # foreman's identity must never mutate its review threads.
        gh, runner = make_github()
        runner.when(
            ["pr", "view", "9"],
            {"number": 9, "author": {"login": "human"}, "labels": [], "body": ""},
        )
        with self.assertRaises(ForemanError):
            gh.resolve_review_thread(9, "T_x")
        with self.assertRaises(ForemanError):
            gh.reply_review_thread(9, "T_x", "note")
        self.assertFalse(runner.called_with_prefix(["api", "graphql"]))

    def test_label_namespace_is_enforced(self):
        gh, runner = make_github()
        runner.when(
            ["pr", "view", "9"],
            {"number": 9, "author": {"login": "bot"}, "labels": [], "body": ""},
        )
        with self.assertRaises(ForemanError):
            gh.label_own_pr(9, add=["priority:high"])

    def test_label_namespace_is_enforced_on_removal(self):
        # #54: stripping a human's label is as out-of-contract as adding one.
        gh, runner = make_github()
        runner.when(
            ["pr", "view", "9"],
            {"number": 9, "author": {"login": "bot"}, "labels": [], "body": ""},
        )
        with self.assertRaises(ForemanError):
            gh.label_own_pr(9, remove=["priority:high"])

    def test_status_comment_edits_only_own_marked_comment(self):
        gh, runner = make_github()
        comments = [
            # A human comment carrying a forged marker must never be edited.
            {"id": 1, "body": STATUS_MARKER + " forged", "user": {"login": "human"}},
            {"id": 2, "body": STATUS_MARKER + " real", "user": {"login": "bot"}},
        ]
        runner.when(
            ["api", "repos/owner/repo/issues/7/comments", "--paginate", "--slurp"],
            [comments],
        )
        runner.when(
            ["api", "--method", "PATCH", "repos/owner/repo/issues/comments/2"], "{}"
        )
        gh.upsert_status_comment(7, STATUS_MARKER + "\nupdated")
        patches = runner.called_with_prefix(["api", "--method", "PATCH"])
        self.assertEqual(len(patches), 1)
        self.assertIn("comments/2", patches[0][3])

    def test_status_comment_requires_marker(self):
        gh, _runner = make_github()
        with self.assertRaises(ForemanError):
            gh.upsert_status_comment(7, "no marker here")


class StatusCommentBody(unittest.TestCase):
    """#90: `-f body=@-` makes gh post the literal string '@-'; only `-F`
    reads the body from stdin. Assert the real marker+status body is sent and
    the argv carries the read-from-stdin idiom, never a literal '-f body=@-'.
    """

    def _assert_reads_body_from_stdin(self, argv, input_text, body):
        # gh must read the body from stdin (`-F body=@-`), not receive '@-'
        # as a literal field value (`-f body=@-`), and the real content must
        # ride stdin.
        self.assertEqual(input_text, body)
        self.assertIn(STATUS_MARKER, input_text)
        self.assertIn("state: dispatched", input_text)
        self.assertIn("body=@-", argv)
        idiom = argv[argv.index("body=@-") - 1]
        self.assertEqual(idiom, "-F", f"expected `-F body=@-`, got `{idiom} body=@-`")
        self.assertNotIn("-f", argv)

    def test_create_branch_posts_real_marker_and_status_body(self):
        gh, runner = make_github()
        # No existing marked comment -> the create (POST) branch runs.
        runner.when(
            ["api", "repos/owner/repo/issues/7/comments", "--paginate", "--slurp"],
            [[]],
        )
        runner.when(["api", "--method", "POST"], "{}")
        body = STATUS_MARKER + "\n| unit | state |\n| 7 | state: dispatched |"
        gh.upsert_status_comment(7, body)
        posts = [
            (argv, text)
            for argv, text in runner.calls
            if argv[:3] == ["api", "--method", "POST"]
        ]
        self.assertEqual(len(posts), 1)
        self._assert_reads_body_from_stdin(*posts[0], body)

    def test_edit_branch_patches_real_marker_and_status_body(self):
        gh, runner = make_github()
        runner.when(
            ["api", "repos/owner/repo/issues/7/comments", "--paginate", "--slurp"],
            [[{"id": 2, "body": STATUS_MARKER + " old", "user": {"login": "bot"}}]],
        )
        runner.when(
            ["api", "--method", "PATCH", "repos/owner/repo/issues/comments/2"], "{}"
        )
        body = STATUS_MARKER + "\n| unit | state |\n| 7 | state: dispatched |"
        gh.upsert_status_comment(7, body)
        patches = [
            (argv, text)
            for argv, text in runner.calls
            if argv[:3] == ["api", "--method", "PATCH"]
        ]
        self.assertEqual(len(patches), 1)
        self._assert_reads_body_from_stdin(*patches[0], body)

    def test_post_vet_correction_posts_real_body(self):
        gh, runner = make_github()
        runner.when(["api", "--method", "POST"], "{}")
        body = STATUS_MARKER + "\nstate: dispatched — correction body"
        gh.post_vet_correction(7, body, human_approved=True)
        posts = [
            (argv, text)
            for argv, text in runner.calls
            if argv[:3] == ["api", "--method", "POST"]
        ]
        self.assertEqual(len(posts), 1)
        self._assert_reads_body_from_stdin(*posts[0], body)


class ForbiddenOperationsAbsent(unittest.TestCase):
    """Merging, closing/reopening/editing issues, deleting others' comments —
    the write contract says these must not exist. Keep them nonexistent."""

    def test_no_forbidden_gh_invocations_in_source(self):
        source = inspect.getsource(github_mod)
        forbidden = [
            r"\"merge\"",  # gh pr merge — never; mergeable/mergeStateStatus fields are fine
            r"'merge'",
            r"issue\W+close",
            r"issue\W+reopen",
            r"issue\W+edit",
            r"issue\W+delete",
            r"mergePullRequest",
            r"enablePullRequestAutoMerge",
        ]
        for pattern in forbidden:
            self.assertIsNone(
                re.search(pattern, source, re.I),
                f"forbidden operation pattern {pattern!r} found in github.py",
            )

    def test_delete_is_confined_to_claim_label_removal(self):
        # #169 introduced the one DELETE foreman issues. It must remain
        # confined to removing a `claim:*` label association — never a comment
        # or an issue. Every DELETE in the module must target a labels
        # endpoint.
        source = inspect.getsource(github_mod)
        deletes = list(re.finditer(r'"DELETE"', source))
        self.assertEqual(
            len(deletes), 1, "exactly one DELETE verb is permitted (claim label)"
        )
        for match in deletes:
            window = source[match.start() : match.start() + 300]
            self.assertIn(
                "/labels/",
                window,
                "DELETE must target a labels endpoint (claim label removal only)",
            )

    def test_no_merge_method_exists(self):
        gh, _runner = make_github()
        for name in dir(gh):
            self.assertNotIn(
                "merge",
                name.lower(),
                f"GitHub facade must not expose a merge-ish method: {name}",
            )


if __name__ == "__main__":
    unittest.main()
