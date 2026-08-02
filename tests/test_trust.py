"""D4 repo predicate and D13 per-unit classification (#14) — fail closed
everywhere the spec says to."""

from __future__ import annotations

import unittest

from foreman import trust as trust_mod
from foreman.capabilities import UNTRUSTED_INPUT
from foreman.config import Config
from foreman.graph import _unit_from_issue
from foreman.inputs import UnitInputs
from tests.fakes import (
    issue_json,
    make_github,
    stub_collaborators,
    stub_content_edits,
    stub_label_events,
)


def armed_unit(issue: dict):
    unit = _unit_from_issue(issue)
    unit.inputs = UnitInputs(mode="labels", armed=True)
    return unit


class RepoPredicate(unittest.TestCase):
    def test_public_repo_is_always_untrusted(self):
        cfg = Config(trusted_actors=["owner"])
        gh, _runner = make_github(cfg, visibility="PUBLIC")
        result = trust_mod.repo_trust(gh, cfg)
        self.assertTrue(result.untrusted)
        self.assertIn("public", result.reason)

    def test_private_with_only_trusted_access_is_trusted(self):
        cfg = Config(trusted_actors=["owner", "evan"])
        gh, runner = make_github(cfg, visibility="PRIVATE")
        stub_collaborators(runner, ["owner", "evan"])
        result = trust_mod.repo_trust(gh, cfg)
        self.assertFalse(result.untrusted)

    def test_private_with_access_beyond_trusted_actors_is_untrusted(self):
        cfg = Config(trusted_actors=["owner"])
        gh, runner = make_github(cfg, visibility="PRIVATE")
        stub_collaborators(runner, ["owner", "drive-by"])
        result = trust_mod.repo_trust(gh, cfg)
        self.assertTrue(result.untrusted)
        self.assertIn("@drive-by", result.reason)

    def test_empty_trusted_actors_fails_closed(self):
        cfg = Config()
        gh, _runner = make_github(cfg, visibility="PRIVATE")
        result = trust_mod.repo_trust(gh, cfg)
        self.assertTrue(result.untrusted)
        self.assertIn("fail closed", result.reason)

    def test_unenumerable_access_fails_closed(self):
        cfg = Config(trusted_actors=["owner"])
        gh, runner = make_github(cfg, visibility="PRIVATE")
        stub_collaborators(runner, [], rc=1)
        result = trust_mod.repo_trust(gh, cfg)
        self.assertTrue(result.untrusted)
        self.assertIn("fail closed", result.reason)

    def test_required_for_injects_untrusted_input(self):
        cfg = Config(required_capabilities=["docker"])
        untrusted = trust_mod.RepoTrust(True, "public")
        trusted = trust_mod.RepoTrust(False, "ok")
        self.assertEqual(
            trust_mod.required_for(cfg, untrusted, None),
            {"docker", UNTRUSTED_INPUT},
        )
        self.assertEqual(trust_mod.required_for(cfg, trusted, None), {"docker"})
        classified = trust_mod.UnitTrust(untrusted_input=True)
        self.assertIn(UNTRUSTED_INPUT, trust_mod.required_for(cfg, trusted, classified))


class ArmingAuthorizes(unittest.TestCase):
    def test_trusted_arming_actor_is_recorded(self):
        cfg = Config(trusted_actors=["owner"])
        gh, runner = make_github(cfg)
        stub_label_events(
            runner,
            7,
            [
                {
                    "label": "foreman:claude",
                    "actor": "owner",
                    "created_at": "2026-07-17T01:00:00Z",
                }
            ],
        )
        stub_content_edits(runner, [])
        unit = armed_unit(issue_json(7, author="owner"))
        result = trust_mod.classify_unit(gh, cfg, unit, "labels")
        self.assertEqual(result.arming_actor, "owner")
        self.assertEqual(result.refusals, [])
        self.assertFalse(result.untrusted_input)

    def test_untrusted_arming_actor_is_refused(self):
        cfg = Config(trusted_actors=["owner"])
        gh, runner = make_github(cfg)
        stub_label_events(
            runner,
            7,
            [
                {
                    "label": "foreman:approved",
                    "actor": "rogue-bot",
                    "created_at": "2026-07-17T01:00:00Z",
                }
            ],
        )
        stub_content_edits(runner, [])
        unit = armed_unit(issue_json(7, author="owner"))
        result = trust_mod.classify_unit(gh, cfg, unit, "labels")
        self.assertTrue(any("rogue-bot" in r for r in result.refusals))

    def test_hold_labels_do_not_count_as_arming(self):
        cfg = Config(trusted_actors=["owner"], require_approval=True)
        gh, runner = make_github(cfg)
        stub_label_events(
            runner,
            7,
            [
                {
                    "label": "foreman:hold",
                    "actor": "rogue",
                    "created_at": "2026-07-17T01:00:00Z",
                }
            ],
        )
        stub_content_edits(runner, [])
        unit = armed_unit(issue_json(7, author="owner"))
        result = trust_mod.classify_unit(gh, cfg, unit, "labels")
        # armed with no attributable ARMING event → fail closed
        self.assertTrue(any("no arming-label event" in r for r in result.refusals))

    def test_fields_mode_arming_fails_closed(self):
        cfg = Config(trusted_actors=["owner"])
        gh, runner = make_github(cfg)
        stub_content_edits(runner, [])
        stub_label_events(runner, 7, [])  # rename attribution reads the timeline
        unit = armed_unit(issue_json(7, author="owner"))
        result = trust_mod.classify_unit(gh, cfg, unit, "fields")
        self.assertTrue(any("fields mode" in r for r in result.refusals))


class AuthorshipClassifies(unittest.TestCase):
    def classify(self, *, author: str, edits: list[dict], arming_actor="owner"):
        cfg = Config(trusted_actors=["owner", "evan"])
        gh, runner = make_github(cfg)
        stub_label_events(
            runner,
            7,
            [
                {
                    "label": "foreman:claude",
                    "actor": arming_actor,
                    "created_at": "2026-07-17T01:00:00Z",
                }
            ],
        )
        stub_content_edits(runner, edits)
        unit = armed_unit(issue_json(7, author=author))
        return trust_mod.classify_unit(gh, cfg, unit, "labels")

    def test_untrusted_author_classifies_not_excludes(self):
        result = self.classify(author="outsider", edits=[])
        self.assertTrue(result.untrusted_input)
        self.assertEqual(result.refusals, [])  # dispatchable where the boundary permits
        self.assertTrue(any("outsider" in c for c in result.contributors))

    def classify_with_rename(self, *, rename_actor: str, renamed_at: str):
        """Titles render into prompts, so renames classify like body edits —
        attribution comes from the same timeline the arming reader uses."""
        cfg = Config(trusted_actors=["owner", "evan"])
        gh, runner = make_github(cfg)
        runner.when(
            [
                "api",
                "repos/owner/repo/issues/7/timeline?per_page=100",
                "--paginate",
                "--slurp",
            ],
            [
                [
                    {
                        "event": "labeled",
                        "label": {"name": "foreman:claude"},
                        "actor": {"login": "owner"},
                        "created_at": "2026-07-17T01:00:00Z",
                    },
                    {
                        "event": "renamed",
                        "actor": {"login": rename_actor},
                        "created_at": renamed_at,
                    },
                ]
            ],
        )
        stub_content_edits(runner, [])
        unit = armed_unit(issue_json(7, author="owner"))
        return trust_mod.classify_unit(gh, cfg, unit, "labels")

    def test_trusted_rename_keeps_the_attestation(self):
        result = self.classify_with_rename(
            rename_actor="evan", renamed_at="2026-07-17T02:00:00Z"
        )
        self.assertFalse(result.untrusted_input)
        self.assertEqual(result.refusals, [])

    def test_untrusted_pre_arming_rename_classifies(self):
        result = self.classify_with_rename(
            rename_actor="outsider", renamed_at="2026-07-17T00:30:00Z"
        )
        self.assertTrue(result.untrusted_input)
        self.assertEqual(result.refusals, [])
        self.assertTrue(any("renamer" in c for c in result.contributors))

    def test_untrusted_post_arming_rename_breaks_the_attestation(self):
        result = self.classify_with_rename(
            rename_actor="outsider", renamed_at="2026-07-17T02:00:00Z"
        )
        self.assertTrue(result.untrusted_input)
        self.assertTrue(any("renamed by untrusted actor" in r for r in result.refusals))

    def test_same_second_untrusted_rename_fails_closed(self):
        # Timeline timestamps have second granularity: a rename in the SAME
        # second as arming is order-unknowable and must break the
        # attestation, not slip through as pre-arming.
        result = self.classify_with_rename(
            rename_actor="outsider", renamed_at="2026-07-17T01:00:00Z"
        )
        self.assertTrue(any("renamed by untrusted" in r for r in result.refusals))

    def test_same_second_untrusted_edit_fails_closed(self):
        result = self.classify(
            author="owner",
            edits=[{"editor": "outsider", "edited_at": "2026-07-17T01:00:00Z"}],
        )
        self.assertTrue(any("re-arm" in r for r in result.refusals))

    def test_trusted_post_arming_edit_keeps_the_attestation(self):
        result = self.classify(
            author="owner",
            edits=[{"editor": "evan", "edited_at": "2026-07-17T02:00:00Z"}],
        )
        self.assertEqual(result.refusals, [])
        self.assertFalse(result.untrusted_input)

    def test_untrusted_post_arming_edit_breaks_the_attestation(self):
        result = self.classify(
            author="owner",
            edits=[{"editor": "outsider", "edited_at": "2026-07-17T02:00:00Z"}],
        )
        self.assertTrue(result.untrusted_input)
        self.assertTrue(any("re-arm" in r for r in result.refusals))

    def test_untrusted_pre_arming_edit_classifies_without_breaking(self):
        result = self.classify(
            author="owner",
            edits=[{"editor": "outsider", "edited_at": "2026-07-17T00:30:00Z"}],
        )
        self.assertTrue(result.untrusted_input)
        self.assertEqual(result.refusals, [])


if __name__ == "__main__":
    unittest.main()
