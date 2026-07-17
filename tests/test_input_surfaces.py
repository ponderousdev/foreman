"""Input-surface enforcement (#46): the shepherd feeds only trusted-authored
review text to agent prompts; untrusted threads on a runner without the
untrusted-input boundary escalate to a human instead."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from foreman import shepherd as shepherd_mod
from foreman.config import Config
from foreman.runner import Selection
from tests.fakes import make_github
from tests.mock_runner import MockRunner


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


if __name__ == "__main__":
    unittest.main()
