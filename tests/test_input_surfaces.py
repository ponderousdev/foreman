"""Input-surface enforcement (#46): the shepherd feeds only trusted-authored
review text to agent prompts; untrusted threads on a runner without the
untrusted-input boundary escalate to a human instead; a fix unit inherits
its branch's classification; and the agent only RECORDS adjudication
dispositions — foreman performs the thread writes."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from foreman import backend as backend_mod
from foreman import shepherd as shepherd_mod
from foreman.config import Config
from foreman.runner import Selection
from tests.fakes import FakeRunner, make_github
from tests.mock_runner import MockRunner


def stub_origin(runner: FakeRunner, number: int, author: str) -> None:
    """The origin-unit reads classify_branch_origin performs: the issue,
    its (empty) content-edit history, and its (empty) rename timeline."""
    runner.when(
        ["issue", "view", str(number)],
        {"number": number, "author": {"login": author}, "subIssues": []},
    )
    runner.when(
        ["api", "graphql"],
        {"data": {"repository": {"issue": {"userContentEdits": {"nodes": []}}}}},
    )
    runner.when(
        [
            "api",
            f"repos/owner/repo/issues/{number}/timeline?per_page=100",
            "--paginate",
            "--slurp",
        ],
        [[]],
    )


def thread(author: str, *, resolved: bool = False, extra_authors=None) -> dict:
    nodes = [{"author": {"login": author}, "body": "please change X"}]
    for other in extra_authors or []:
        nodes.append({"author": {"login": other}, "body": "+1"})
    return {
        "id": f"t-{author}",
        "isResolved": resolved,
        "path": "src/x.py",
        "comments": {"nodes": nodes},
    }


class ThreadTrust(unittest.TestCase):
    def test_all_trusted_authors_is_trusted(self):
        cfg = Config(trusted_actors=["reviewer", "evan"])
        gh, _r = make_github(cfg)
        self.assertTrue(
            shepherd_mod._thread_trusted(
                gh, cfg, thread("reviewer", extra_authors=["evan"])
            )
        )

    def test_foreman_itself_counts_as_trusted(self):
        cfg = Config(trusted_actors=[])
        gh, _r = make_github(cfg)  # viewer == "bot"
        self.assertTrue(shepherd_mod._thread_trusted(gh, cfg, thread("bot")))

    def test_one_untrusted_voice_taints_the_thread(self):
        cfg = Config(trusted_actors=["reviewer"])
        gh, _r = make_github(cfg)
        self.assertFalse(
            shepherd_mod._thread_trusted(
                gh, cfg, thread("reviewer", extra_authors=["drive-by"])
            )
        )

    def test_empty_thread_is_not_trusted(self):
        cfg = Config(trusted_actors=["reviewer"])
        gh, _r = make_github(cfg)
        empty = {"id": "t", "comments": {"nodes": []}}
        self.assertFalse(shepherd_mod._thread_trusted(gh, cfg, empty))

    def test_unfetched_comments_fail_closed(self):
        # comments(first: 50): a 51st commenter is invisible to the trust
        # check, so a fuller-than-fetched thread cannot be attested — taint.
        cfg = Config(trusted_actors=["reviewer"])
        gh, _r = make_github(cfg)
        big = thread("reviewer")
        big["comments"]["totalCount"] = 51
        self.assertFalse(shepherd_mod._thread_trusted(gh, cfg, big))


class UntrustedThreadEscalates(unittest.TestCase):
    """A local runner (no untrusted-input) must never read a world-writable
    review comment into a prompt. This drives shepherd_pr just far enough to
    reach the review-thread branch: green checks, clean mergeable-but-for
    the thread."""

    def _status(self):
        return {
            "number": 10,
            "url": "https://example.invalid/pr/10",
            "title": "feat: x",
            "headRefName": "foreman/feat/5-x",
            "statusCheckRollup": [
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "verify"}
            ],
            "mergeStateStatus": "CLEAN",
            "mergeable": "MERGEABLE",
            "labels": [],
        }

    def test_untrusted_thread_escalates_on_local(self):
        cfg = Config(trusted_actors=["reviewer"])
        gh, runner = make_github(cfg)
        runner.when(["pr", "view", "10"], self._status())
        stub_origin(runner, 5, "reviewer")

        # Inject review_threads directly: one untrusted-authored, unresolved.
        gh.review_threads = lambda number: [thread("drive-by-attacker")]  # type: ignore

        selection = Selection(
            runner=MockRunner(caps=set()),  # local: no untrusted-input
            make_handoff=lambda w, h: None,
            refusal=lambda req: None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            work = shepherd_mod.shepherd_pr(
                gh, cfg, Path(tmp), selection, {"number": 10, "_unit": 5}, []
            )
        self.assertEqual(work.state, "escalated")
        self.assertIn("untrusted", work.detail)
        self.assertIn("#46", work.detail)

    def test_untrusted_thread_is_adjudicable_where_boundary_exists(self):
        # A runner advertising untrusted-input (sprite, v2.1) MAY read the
        # thread — the boundary contains a compromised agent. Here we only
        # assert the escalation guard does NOT fire; the adjudication path
        # itself needs a full agent run, out of scope for this unit test.
        cfg = Config(trusted_actors=["reviewer"])
        gh, _runner = make_github(cfg)
        gh.review_threads = lambda number: [thread("drive-by")]  # type: ignore
        selection = Selection(
            runner=MockRunner(caps={"untrusted-input", "ports"}),
            make_handoff=lambda w, h: None,
            refusal=lambda req: None,
        )
        # Only exercise the guard predicate, not the whole PR path.
        untrusted = [
            t
            for t in gh.review_threads(10)
            if not shepherd_mod._thread_trusted(gh, cfg, t)
        ]
        self.assertTrue(untrusted)
        self.assertIn("untrusted-input", selection.runner.capabilities())


class UntrustedOriginBranchEscalates(unittest.TestCase):
    """#46: a fix unit inherits its branch's classification. Red CI on an
    untrusted-origin branch (public repo ⇒ D4 untrusted) escalates on a
    runner without untrusted-input instead of feeding its log text to an
    agent."""

    def _red_status(self):
        return {
            "number": 10,
            "url": "https://example.invalid/pr/10",
            "title": "feat: x",
            "headRefName": "foreman/feat/5-x",
            "statusCheckRollup": [
                {"status": "COMPLETED", "conclusion": "FAILURE", "name": "verify"}
            ],
            "mergeStateStatus": "CLEAN",
            "mergeable": "MERGEABLE",
            "labels": [],
        }

    def test_red_ci_on_untrusted_origin_escalates(self):
        cfg = Config(trusted_actors=["evan"])
        gh, runner = make_github(cfg)  # visibility PUBLIC ⇒ repo untrusted
        runner.when(["pr", "view", "10"], self._red_status())
        stub_origin(runner, 5, "rando")

        selection = Selection(
            runner=MockRunner(caps=set()),
            make_handoff=lambda w, h: None,
            refusal=lambda req: (
                "requires untrusted-input" if "untrusted-input" in req else None
            ),
        )
        resumed: list[int] = []
        original = shepherd_mod._resume_agent
        shepherd_mod._resume_agent = (  # type: ignore[assignment]
            lambda *a, **k: resumed.append(1)
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                work = shepherd_mod.shepherd_pr(
                    gh, cfg, Path(tmp), selection, {"number": 10, "_unit": 5}, []
                )
        finally:
            shepherd_mod._resume_agent = original  # type: ignore[assignment]
        self.assertEqual(work.state, "escalated")
        self.assertIn("#46", work.detail)
        self.assertIn("untrusted-origin", work.detail)
        self.assertEqual(resumed, [])


class _StubHandoff:
    def is_clean(self):
        return True

    def commits_ahead(self, ref):
        return 0

    def push(self, remote, branch, first=False):
        raise AssertionError("no push expected in this test")


class AdjudicationWritePath(unittest.TestCase):
    """The agent records dispositions in the sidecar; foreman posts each
    note and resolves each thread through its own guarded write surface."""

    def _green_status(self):
        return {
            "number": 10,
            "url": "https://example.invalid/pr/10",
            "title": "feat: x",
            "headRefName": "foreman/feat/5-x",
            "statusCheckRollup": [
                {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "verify"}
            ],
            "mergeStateStatus": "CLEAN",
            "mergeable": "MERGEABLE",
            "labels": [],
        }

    def _drive(self, tmp: str, gh, cfg, sidecar, runner=None) -> shepherd_mod.PrWork:
        """Run shepherd_pr through the adjudicate branch with the agent
        resume and worktree patched out; `sidecar` is what the fake agent
        writes (None to simulate an agent that wrote nothing)."""
        selection = Selection(
            runner=runner or MockRunner(caps={"untrusted-input"}),
            make_handoff=lambda w, h: _StubHandoff(),
            refusal=lambda req: None,
        )
        root = Path(tmp)

        def fake_resume(gh_, cfg_, root_, selection_, work_, prompt_name, tokens):
            assert prompt_name == "shepherd-adjudicate"
            assert "ADJUDICATION_FILE" in tokens
            if sidecar is not None:
                Path(tokens["ADJUDICATION_FILE"]).write_text(
                    json.dumps(sidecar), encoding="utf-8"
                )
            return backend_mod.BackendResult(returncode=0)

        original_resume = shepherd_mod._resume_agent
        original_worktree = shepherd_mod._ensure_worktree
        shepherd_mod._resume_agent = fake_resume  # type: ignore[assignment]
        shepherd_mod._ensure_worktree = (  # type: ignore[assignment]
            lambda cfg_, root_, unit, branch, remote: root
        )
        try:
            return shepherd_mod.shepherd_pr(
                gh, cfg, root, selection, {"number": 10, "_unit": 5}, []
            )
        finally:
            shepherd_mod._resume_agent = original_resume  # type: ignore[assignment]
            shepherd_mod._ensure_worktree = original_worktree  # type: ignore[assignment]

    def _wire_writes(self, gh, threads_by_id):
        """Replace the two guarded mutations with recorders; resolving marks
        the thread resolved so the completeness re-check sees it."""
        replies: list[tuple[int, str, str]] = []
        resolves: list[str] = []
        gh.reply_review_thread = (  # type: ignore[assignment]
            lambda pr, tid, body: replies.append((pr, tid, body))
        )

        def resolve(pr, tid):
            resolves.append(tid)
            threads_by_id[tid]["isResolved"] = True

        gh.resolve_review_thread = resolve  # type: ignore[assignment]
        return replies, resolves

    def test_foreman_performs_the_recorded_writes(self):
        cfg = Config(trusted_actors=["reviewer"])
        gh, runner = make_github(cfg)
        runner.when(["pr", "view", "10"], self._green_status())
        stub_origin(runner, 5, "reviewer")
        the_thread = thread("reviewer")
        gh.review_threads = lambda number: [the_thread]  # type: ignore
        replies, resolves = self._wire_writes(gh, {the_thread["id"]: the_thread})

        sidecar = {
            "schema": 1,
            "dispositions": [
                {
                    "thread_id": the_thread["id"],
                    "disposition": "declined",
                    "note": "covered by the typecheck",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            work = self._drive(tmp, gh, cfg, sidecar)
        self.assertEqual(work.state, "adjudicated")
        marker = shepherd_mod._disposition_marker(the_thread["id"])
        self.assertEqual(
            replies,
            [(10, the_thread["id"], "covered by the typecheck\n\n" + marker)],
        )
        self.assertEqual(resolves, [the_thread["id"]])

    def test_applied_with_commit_on_branch_resolves(self):
        cfg = Config(trusted_actors=["reviewer"])
        gh, runner = make_github(cfg)
        runner.when(["pr", "view", "10"], self._green_status())
        stub_origin(runner, 5, "reviewer")
        the_thread = thread("reviewer")
        gh.review_threads = lambda number: [the_thread]  # type: ignore
        replies, resolves = self._wire_writes(gh, {the_thread["id"]: the_thread})

        sidecar = {
            "schema": 1,
            "dispositions": [
                {
                    "thread_id": the_thread["id"],
                    "disposition": "applied",
                    "note": "applied in abc1234",
                }
            ],
        }
        # Default MockRunner.exec returns rc 0 → cat-file + is-ancestor pass.
        with tempfile.TemporaryDirectory() as tmp:
            work = self._drive(tmp, gh, cfg, sidecar)
        self.assertEqual(work.state, "adjudicated")
        self.assertEqual(resolves, [the_thread["id"]])

    def test_applied_commit_missing_from_branch_escalates(self):
        from foreman.runner import ExecResult

        cfg = Config(trusted_actors=["reviewer"])
        gh, runner = make_github(cfg)
        runner.when(["pr", "view", "10"], self._green_status())
        stub_origin(runner, 5, "reviewer")
        the_thread = thread("reviewer")
        gh.review_threads = lambda number: [the_thread]  # type: ignore
        replies, resolves = self._wire_writes(gh, {the_thread["id"]: the_thread})

        mock = MockRunner(caps={"untrusted-input"})
        mock.exec = (  # type: ignore[assignment]
            lambda handle, cmd: ExecResult(returncode=1, stdout="", stderr="")
        )
        sidecar = {
            "schema": 1,
            "dispositions": [
                {
                    "thread_id": the_thread["id"],
                    "disposition": "applied",
                    "note": "applied in abc1234",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            work = self._drive(tmp, gh, cfg, sidecar, runner=mock)
        self.assertEqual(work.state, "escalated")
        self.assertIn("not on the branch", work.detail)
        self.assertEqual(replies, [])
        self.assertEqual(resolves, [])

    def test_duplicate_reply_is_not_reposted(self):
        cfg = Config(trusted_actors=["reviewer"])
        gh, runner = make_github(cfg)
        runner.when(["pr", "view", "10"], self._green_status())
        stub_origin(runner, 5, "reviewer")
        the_thread = thread("reviewer")
        # A previous tick already posted this exact note (author == viewer
        # "bot") but died before resolving; the retry must not repost. The
        # note is worded DIFFERENTLY here — dedupe keys on the marker, not
        # the text, because a re-run agent rewords its notes.
        the_thread["comments"]["nodes"].append(
            {
                "author": {"login": "bot"},
                "body": "the typecheck already covers this\n\n"
                + shepherd_mod._disposition_marker(the_thread["id"]),
            }
        )
        gh.review_threads = lambda number: [the_thread]  # type: ignore
        replies, resolves = self._wire_writes(gh, {the_thread["id"]: the_thread})

        sidecar = {
            "schema": 1,
            "dispositions": [
                {
                    "thread_id": the_thread["id"],
                    "disposition": "declined",
                    "note": "covered by the typecheck",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            work = self._drive(tmp, gh, cfg, sidecar)
        self.assertEqual(work.state, "adjudicated")
        self.assertEqual(replies, [])
        self.assertEqual(resolves, [the_thread["id"]])

    def test_unrendered_thread_id_escalates(self):
        # 21 unresolved threads: only 20 are rendered into the prompt, and
        # only those 20 may be dispositioned — an agent must not be able to
        # steer foreman's write token at a thread it was never shown.
        cfg = Config(trusted_actors=["reviewer"])
        gh, runner = make_github(cfg)
        runner.when(["pr", "view", "10"], self._green_status())
        stub_origin(runner, 5, "reviewer")
        threads = [thread(f"r{i}") for i in range(21)]
        gh.review_threads = lambda number: list(threads)  # type: ignore
        replies, resolves = self._wire_writes(gh, {t["id"]: t for t in threads})

        sidecar = {
            "schema": 1,
            "dispositions": [
                {"thread_id": threads[20]["id"], "disposition": "declined", "note": "n"}
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            work = self._drive(tmp, gh, cfg, sidecar)
        self.assertEqual(work.state, "escalated")
        self.assertIn("unknown thread id", work.detail)
        self.assertEqual(replies, [])
        self.assertEqual(resolves, [])

    def test_unknown_thread_id_escalates_without_writes(self):
        cfg = Config(trusted_actors=["reviewer"])
        gh, runner = make_github(cfg)
        runner.when(["pr", "view", "10"], self._green_status())
        stub_origin(runner, 5, "reviewer")
        the_thread = thread("reviewer")
        gh.review_threads = lambda number: [the_thread]  # type: ignore
        replies, resolves = self._wire_writes(gh, {the_thread["id"]: the_thread})

        sidecar = {
            "schema": 1,
            "dispositions": [
                {"thread_id": "t-bogus", "disposition": "declined", "note": "n"}
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            work = self._drive(tmp, gh, cfg, sidecar)
        self.assertEqual(work.state, "escalated")
        self.assertIn("unknown thread id", work.detail)
        self.assertEqual(replies, [])
        self.assertEqual(resolves, [])

    def test_missing_sidecar_escalates(self):
        cfg = Config(trusted_actors=["reviewer"])
        gh, runner = make_github(cfg)
        runner.when(["pr", "view", "10"], self._green_status())
        stub_origin(runner, 5, "reviewer")
        gh.review_threads = lambda number: [thread("reviewer")]  # type: ignore
        with tempfile.TemporaryDirectory() as tmp:
            work = self._drive(tmp, gh, cfg, None)
        self.assertEqual(work.state, "escalated")
        self.assertIn("adjudication sidecar invalid", work.detail)


if __name__ == "__main__":
    unittest.main()
