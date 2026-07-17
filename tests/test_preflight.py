"""Preflight probes (#15): every control proven, every probe bounded to a
scratch ref it cleans up, an unexpected success failing loudly. A fake gh
records calls and lets a test say "this write unexpectedly succeeded"."""

from __future__ import annotations

import json
import unittest

from foreman import preflight as pf

SLUG = "owner/repo"
MAIN_SHA = "a" * 40
PROBE_SHA = "b" * 40


class FakeGh:
    """Rule-driven gh double. A rule matches when ALL its substrings appear
    in the joined argv; first match wins; the default is DENY (rc=1) — a
    fail-closed double, so a token can only do what a test explicitly
    grants. `calls` records everything for cleanup assertions."""

    # Default denial reads like a real GitHub ruleset/permission block so the
    # fail-closed classifier recognizes it (a bare "denied" would now be an
    # INCONCLUSIVE fail, which is the point of the classifier).
    DENIAL_STDERR = "gh: Resource not accessible by personal access token (HTTP 403)"

    def __init__(self):
        self.rules: list[tuple[list[str], int, str, str]] = []
        self.calls: list[list[str]] = []

    def when(self, needles: list[str], rc: int = 0, out: object = "", err: str = ""):
        self.rules.append(
            (
                needles,
                rc,
                out if isinstance(out, str) else json.dumps(out),
                err or (self.DENIAL_STDERR if rc != 0 else ""),
            )
        )
        return self

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(args)
        joined = " ".join(args)
        for needles, rc, out, err in self.rules:
            if all(needle in joined for needle in needles):
                return rc, out, err
        return 1, "", self.DENIAL_STDERR

    def attempted(self, needle: str) -> list[list[str]]:
        return [c for c in self.calls if any(needle in part for part in c)]


def base_write() -> FakeGh:
    """A correctly-locked-down write token: identity ok, PR rule present,
    base + probe-tag shas resolve, ordinary branches can be created and
    deleted (contents:write) — but tag creation, workflow edits, and probe
    tag move/delete are all denied by the rulesets."""
    gh = FakeGh()
    gh.when(["user", ".login"], out="evanharmon1-bot\n")
    gh.when(["rules/branches/main"], out=[{"type": "pull_request"}])
    gh.when(["git/ref/heads/main"], out={"object": {"sha": MAIN_SHA}})
    gh.when([f"git/ref/tags/{pf.PROBE_TAG}"], out={"object": {"sha": PROBE_SHA}})
    # Ordinary branch lifecycle is allowed (a real write token can do this).
    gh.when(["--method POST", "ref=refs/heads/"], out="{}")
    gh.when(["--method DELETE", "refs/heads/"], out="")
    return gh


def denied_read() -> FakeGh:
    return FakeGh()  # bare double denies all — a read token that cannot write


def run(write: FakeGh, read: FakeGh, *, login="evanharmon1-bot"):
    return pf.run_preflight(
        slug=SLUG,
        default_branch="main",
        expected_login=login,
        write=write,
        read=read,
    )


def probe(probes, name):
    return next(p for p in probes if p.name == name)


class HappyPath(unittest.TestCase):
    def test_all_controls_hold(self):
        probes = run(base_write(), denied_read())
        self.assertTrue(all(p.ok for p in probes), [p for p in probes if not p.ok])
        names = {p.name for p in probes}
        self.assertIn("write-token login", names)
        self.assertIn("default branch requires PRs", names)
        self.assertIn("read token cannot write", names)
        self.assertIn("write token cannot edit workflows", names)
        self.assertIn("write token cannot create version tags", names)
        self.assertIn("write token cannot move version tags", names)
        self.assertIn("write token cannot delete version tags", names)

    def test_render_names_the_operator_tier(self):
        text = pf.render(run(base_write(), denied_read()))
        self.assertIn("Operator tier", text)
        self.assertIn("bypass", text)


class IdentityAndRules(unittest.TestCase):
    def test_wrong_login_fails(self):
        probes = run(base_write(), denied_read(), login="someone-else")
        self.assertFalse(probe(probes, "write-token login").ok)

    def test_identity_failure_short_circuits_all_mutation_probes(self):
        # A wrong/privileged token must never reach the ref/workflow/tag
        # probes. Only the identity probe should run.
        write = base_write()
        probes = run(write, denied_read(), login="someone-else")
        self.assertEqual([p.name for p in probes], ["write-token login"])
        # No mutation was attempted with the mismatched token.
        self.assertEqual(write.attempted("--method"), [])

    def test_unset_expected_login_short_circuits(self):
        write = base_write()
        probes = run(write, denied_read(), login="")
        self.assertEqual(len(probes), 1)
        self.assertFalse(probes[0].ok)
        self.assertEqual(write.attempted("--method"), [])

    def test_missing_pr_rule_fails(self):
        write = base_write()
        write.rules = [r for r in write.rules if r[0] != ["rules/branches/main"]]
        write.when(["rules/branches/main"], out=[])  # no pull_request rule
        probes = run(write, denied_read())
        self.assertFalse(probe(probes, "default branch requires PRs").ok)


class UnexpectedWritesFailLoudAndCleanUp(unittest.TestCase):
    def test_read_token_that_can_write_fails_and_is_cleaned_up(self):
        read = FakeGh()
        read.when(["--method POST", "git/refs"], out="{}")  # should not be possible
        read.when(["--method DELETE"], out="")  # cleanup succeeds
        probes = run(base_write(), read)
        p = probe(probes, "read token cannot write")
        self.assertFalse(p.ok)
        self.assertTrue(read.attempted("DELETE"))  # scratch ref cleaned up
        self.assertIn("cleaned up", p.detail)

    def test_workflow_edit_success_fails_loud_and_deletes_branch(self):
        write = base_write()
        write.when(
            ["--method PUT", "contents/.github/workflows"], out="{}"
        )  # push unexpectedly allowed
        probes = run(write, denied_read())
        p = probe(probes, "write token cannot edit workflows")
        self.assertFalse(p.ok)
        self.assertTrue(write.attempted("content="))  # a file was pushed
        # scratch branch is torn down even on the failure path
        self.assertTrue(any("wf-probe" in part for c in write.calls for part in c))
        self.assertTrue(write.attempted("DELETE"))

    def test_transport_error_is_inconclusive_not_a_pass(self):
        # A 5xx / network error on a write probe must FAIL closed, never be
        # accepted as "cannot write" (CodeRabbit critical).
        read = FakeGh()
        read.when(
            ["--method POST", "git/refs"],
            rc=1,
            err="gh: Something went wrong (HTTP 502)",
        )
        probes = run(base_write(), read)
        p = probe(probes, "read token cannot write")
        self.assertFalse(p.ok)
        self.assertIn("INCONCLUSIVE", p.detail)

    def test_explicit_denial_passes(self):
        # A 403/ruleset denial is the ONLY thing that passes a write probe.
        read = FakeGh()
        read.when(
            ["--method POST", "git/refs"],
            rc=1,
            err="gh: Resource not accessible (HTTP 403)",
        )
        probes = run(base_write(), read)
        self.assertTrue(probe(probes, "read token cannot write").ok)

    def test_workflow_cleanup_failure_is_reported_not_assumed(self):
        write = base_write()
        write.when(["--method PUT", "contents/.github/workflows"], out="{}")
        # The scratch-branch delete fails → must say LEFT BEHIND, not "deleted".
        # Prepend so it wins over base_write's permissive branch-delete rule.
        write.rules.insert(
            0,
            (
                ["--method DELETE", "refs/heads/foreman-preflight-wf-probe"],
                1,
                "",
                "boom",
            ),
        )
        probes = run(write, denied_read())
        p = probe(probes, "write token cannot edit workflows")
        self.assertFalse(p.ok)
        self.assertIn("LEFT BEHIND", p.detail)

    def test_workflow_probe_content_is_inert(self):
        # No YAML trigger key: a `\non:` (or top-of-file `on:`) block is what
        # would arm the file. The comment prose is not YAML and cannot.
        self.assertNotIn("\non:", pf.INERT_WORKFLOW_YAML)
        self.assertFalse(pf.INERT_WORKFLOW_YAML.startswith("on:"))
        self.assertNotIn("jobs:", pf.INERT_WORKFLOW_YAML)

    def test_tag_creation_success_fails_and_cleans_the_scratch_tag(self):
        write = base_write()
        write.when(["--method POST", "ref=refs/tags/"], out="{}")
        write.when(["--method DELETE", "refs/tags/"], out="")
        probes = run(write, denied_read())
        p = probe(probes, "write token cannot create version tags")
        self.assertFalse(p.ok)
        self.assertTrue(write.attempted("ref=refs/tags/v0.0.0-probe-"))
        self.assertIn("cleaned up", p.detail)

    def test_tag_move_success_restores_the_probe_tag(self):
        write = base_write()
        write.when(["--method PATCH", f"git/refs/tags/{pf.PROBE_TAG}"], out="{}")
        probes = run(write, denied_read())
        p = probe(probes, "write token cannot move version tags")
        self.assertFalse(p.ok)
        restores = [
            c
            for c in write.calls
            if any(f"sha={PROBE_SHA}" in part for part in c)
            and any(f"tags/{pf.PROBE_TAG}" in part for part in c)
        ]
        self.assertTrue(restores)  # tag restored to its original sha

    def test_deleted_probe_tag_without_recreate_names_operator_action(self):
        write = base_write()
        write.when(
            ["--method DELETE", f"git/refs/tags/{pf.PROBE_TAG}"], out=""
        )  # deletion unexpectedly allowed; creation stays denied
        probes = run(write, denied_read())
        p = probe(probes, "write token cannot delete version tags")
        self.assertFalse(p.ok)
        self.assertIn("operator", p.detail.lower())


class MissingProbeTag(unittest.TestCase):
    def test_absent_probe_tag_is_an_operator_action(self):
        write = FakeGh()
        write.when(["user", ".login"], out="evanharmon1-bot\n")
        write.when(["rules/branches/main"], out=[{"type": "pull_request"}])
        write.when(["git/ref/heads/main"], out={"object": {"sha": MAIN_SHA}})
        write.when(["--method POST", "ref=refs/heads/"], out="{}")
        write.when(["--method DELETE", "refs/heads/"], out="")
        # tags/v0.0.0-probe resolves to nothing (denied by default).
        probes = run(write, denied_read())
        p = probe(probes, "write token cannot move or delete version tags")
        self.assertFalse(p.ok)
        self.assertIn(pf.PROBE_TAG, p.detail)


if __name__ == "__main__":
    unittest.main()
