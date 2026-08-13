"""The consumer claim contract (#169): family resolution from the consumer's
agent-registry.json, the never-mint skip, the acquire/release round trip over
the fake gh transport, and the parse shape a consumer reconciler reads back.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from foreman import claim
from foreman.config import Config
from foreman.graph import Unit
from tests.fakes import FakeRunner, make_github

UNIT_NUMBER = 7


def unit() -> Unit:
    return Unit(
        number=UNIT_NUMBER,
        title="U7",
        state="OPEN",
        state_reason=None,
        body="",
        url="",
        labels=[],
        issue_type=None,
        milestone=None,
        parent=None,
    )


def registry(tmp: Path, data: object) -> Path:
    (tmp / claim.REGISTRY_FILE).write_text(json.dumps(data), encoding="utf-8")
    return tmp


def stub_labels(runner: FakeRunner, names: list[str]) -> None:
    runner.when(["label", "list"], [{"name": name} for name in names])


def stub_comments(runner: FakeRunner, comments: list[dict]) -> None:
    runner.when(
        [
            "api",
            f"repos/owner/repo/issues/{UNIT_NUMBER}/comments",
            "--paginate",
            "--slurp",
        ],
        [comments],
    )


def own_claim_comment(family: str = "claude", *, comment_id: int = 2) -> dict:
    body = claim._record_body(family, unit(), "claude", "foreman/feat/7-x")
    return {"id": comment_id, "body": body, "user": {"login": "bot"}}


class ResolveFamily(unittest.TestCase):
    def test_flat_string_mapping(self):
        with tempfile.TemporaryDirectory() as raw:
            root = registry(Path(raw), {"claude": "claude", "codex-cli": "codex"})
            self.assertEqual(claim.resolve_family(root, "claude"), "claude")
            self.assertEqual(claim.resolve_family(root, "codex-cli"), "codex")

    def test_harness_family_of_claude_code_backend(self):
        with tempfile.TemporaryDirectory() as raw:
            root = registry(Path(raw), {"harnesses": {"glm": {"claim_family": "glm"}}})
            self.assertEqual(claim.resolve_family(root, "claude-code-glm"), "glm")

    def test_missing_registry_is_none(self):
        with tempfile.TemporaryDirectory() as raw:
            self.assertIsNone(claim.resolve_family(Path(raw), "claude"))

    def test_malformed_registry_is_none_not_raise(self):
        with tempfile.TemporaryDirectory() as raw:
            (Path(raw) / claim.REGISTRY_FILE).write_text("{not json", encoding="utf-8")
            self.assertIsNone(claim.resolve_family(Path(raw), "claude"))

    def test_no_mapping_for_backend_is_none(self):
        with tempfile.TemporaryDirectory() as raw:
            root = registry(Path(raw), {"claude": "claude"})
            self.assertIsNone(claim.resolve_family(root, "codex-cli"))


class AcquireWritesWhenVocabularyExists(unittest.TestCase):
    def _acquire(self, root: Path):
        gh, runner = make_github()
        stub_labels(runner, ["claim:claude", "bug"])
        stub_comments(runner, [])  # no prior claim record -> create
        runner.when(
            [
                "api",
                "--method",
                "POST",
                f"repos/owner/repo/issues/{UNIT_NUMBER}/labels",
            ],
            "",
        )
        runner.when(
            [
                "api",
                "--method",
                "POST",
                f"repos/owner/repo/issues/{UNIT_NUMBER}/comments",
            ],
            "{}",
        )
        logs: list[str] = []
        claim.acquire(
            gh,
            Config(),
            root,
            unit(),
            backend_name="claude",
            branch="foreman/feat/7-x",
            log=logs.append,
        )
        return runner, logs

    def test_label_and_record_are_written(self):
        with tempfile.TemporaryDirectory() as raw:
            root = registry(Path(raw), {"claude": "claude"})
            runner, logs = self._acquire(root)
        adds = runner.called_with_prefix(
            ["api", "--method", "POST", f"repos/owner/repo/issues/{UNIT_NUMBER}/labels"]
        )
        self.assertEqual(len(adds), 1)
        self.assertIn("labels[]=claim:claude", adds[0])
        posts = [
            text
            for argv, text in runner.calls
            if argv[:4]
            == [
                "api",
                "--method",
                "POST",
                f"repos/owner/repo/issues/{UNIT_NUMBER}/comments",
            ]
        ]
        self.assertEqual(len(posts), 1)
        record = claim.parse_record(posts[0])
        self.assertEqual(record["family"], "claude")
        self.assertEqual(record["state"], "active")
        self.assertEqual(record["claim"], "claim:claude")
        self.assertTrue(any("claim acquired" in line for line in logs))


class AcquireSkipsCleanly(unittest.TestCase):
    def test_skip_when_no_registry_mapping(self):
        with tempfile.TemporaryDirectory() as raw:
            gh, runner = make_github()
            stub_labels(runner, ["claim:claude"])
            logs: list[str] = []
            claim.acquire(
                gh,
                Config(),
                Path(raw),  # no agent-registry.json
                unit(),
                backend_name="claude",
                branch="b",
                log=logs.append,
            )
        # Skipped before resolving a label -> repo_labels never read, no writes.
        self.assertEqual(runner.called_with_prefix(["api", "--method", "POST"]), [])
        self.assertTrue(any("claim skipped" in line for line in logs))

    def test_skip_when_label_undefined_never_mints(self):
        with tempfile.TemporaryDirectory() as raw:
            root = registry(Path(raw), {"claude": "claude"})
            gh, runner = make_github()
            stub_labels(runner, ["bug", "enhancement"])  # no claim:claude
            logs: list[str] = []
            claim.acquire(
                gh,
                Config(),
                root,
                unit(),
                backend_name="claude",
                branch="b",
                log=logs.append,
            )
        self.assertEqual(runner.called_with_prefix(["api", "--method", "POST"]), [])
        self.assertTrue(any("never minting" in line for line in logs), logs)

    def test_write_failure_is_non_fatal(self):
        with tempfile.TemporaryDirectory() as raw:
            root = registry(Path(raw), {"claude": "claude"})
            gh, runner = make_github()
            stub_labels(runner, ["claim:claude"])
            runner.when(
                [
                    "api",
                    "--method",
                    "POST",
                    f"repos/owner/repo/issues/{UNIT_NUMBER}/labels",
                ],
                "boom",
                rc=1,
            )
            logs: list[str] = []
            # Must not raise even though the label POST fails.
            claim.acquire(
                gh,
                Config(),
                root,
                unit(),
                backend_name="claude",
                branch="b",
                log=logs.append,
            )
        self.assertTrue(any("claim write failed" in line for line in logs))


class ReleaseIsExactInverse(unittest.TestCase):
    def test_release_reads_family_and_removes_exactly_that_label(self):
        gh, runner = make_github()
        stub_comments(runner, [own_claim_comment("glm")])
        runner.when(["api", "--method", "DELETE"], "")
        runner.when(
            ["api", "--method", "PATCH", "repos/owner/repo/issues/comments/2"], "{}"
        )
        logs: list[str] = []
        claim.release(gh, unit(), log=logs.append)
        deletes = runner.called_with_prefix(["api", "--method", "DELETE"])
        self.assertEqual(len(deletes), 1)
        self.assertIn(
            f"repos/owner/repo/issues/{UNIT_NUMBER}/labels/claim%3Aglm", deletes[0]
        )
        patches = [
            text
            for argv, text in runner.calls
            if argv[:3] == ["api", "--method", "PATCH"]
        ]
        self.assertEqual(len(patches), 1)
        released = claim.parse_record(patches[0])
        self.assertEqual(released["state"], "released")
        self.assertEqual(released["family"], "glm")

    def test_release_without_a_record_is_a_noop(self):
        gh, runner = make_github()
        stub_comments(runner, [])  # no claim record
        logs: list[str] = []
        claim.release(gh, unit(), log=logs.append)
        self.assertEqual(runner.called_with_prefix(["api", "--method", "DELETE"]), [])
        self.assertEqual(runner.called_with_prefix(["api", "--method", "PATCH"]), [])


class ParseRoundTrip(unittest.TestCase):
    def test_consumer_reads_family_and_state_from_the_marker(self):
        body = claim._record_body(
            "kimi", unit(), "claude-code-kimi", "foreman/feat/7-x"
        )
        record = claim.parse_record(body)
        self.assertEqual(record["family"], "kimi")
        self.assertEqual(record["state"], "active")
        self.assertEqual(record["unit"], UNIT_NUMBER)
        self.assertEqual(record["branch"], "foreman/feat/7-x")

    def test_no_marker_is_none(self):
        self.assertIsNone(claim.parse_record("just a normal comment"))


if __name__ == "__main__":
    unittest.main()
