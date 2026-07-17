"""Capability model (#28): unknown names refused, refusals name the absent
capability and a compatible runner, gate composition skips what the
environment lacks (#29)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from foreman import capabilities as capabilities_mod
from foreman import gate, verify
from foreman.config import Config
from foreman.util import ForemanError


class Names(unittest.TestCase):
    def test_unknown_names_are_config_errors(self):
        with self.assertRaises(ForemanError) as ctx:
            capabilities_mod.validate_names(["port"], where="[verify]")
        message = str(ctx.exception)
        self.assertIn("port", message)
        self.assertIn("ports", message)  # the known list is named
        self.assertIn("silently skipped", message)

    def test_known_names_pass(self):
        capabilities_mod.validate_names(
            ["docker", "ports", "untrusted-input"], where="x"
        )


class Refusals(unittest.TestCase):
    def test_met_requirements_are_none(self):
        self.assertIsNone(capabilities_mod.refusal({"docker"}, {"docker"}, "local"))
        self.assertIsNone(capabilities_mod.refusal(set(), set(), "local"))

    def test_ports_under_local_names_the_gap_and_the_milestone(self):
        message = capabilities_mod.refusal({"ports"}, {"docker"}, "local")
        assert message is not None
        self.assertIn("ports", message)
        self.assertIn("local", message)
        # No runner ships ports in v2.0; the milestones make that honest.
        self.assertIn("v2.1", message)

    def test_untrusted_input_names_sprite_as_the_compatible_runner(self):
        message = capabilities_mod.refusal({"untrusted-input"}, {"docker"}, "local")
        assert message is not None
        self.assertIn("untrusted-input", message)
        self.assertIn("sprite", message)
        self.assertIn("v2.1", message)
        self.assertNotIn("v2.2", message)  # docker never advertises it (D12)

    def test_impossible_requirement_says_so(self):
        message = capabilities_mod.refusal(
            {"docker", "untrusted-input"}, set(), "local"
        )
        assert message is not None
        # No planned runner advertises docker+untrusted-input together (D5/D12).
        self.assertIn("no currently available runner", message)

    def test_assert_repo_requirements_raises_on_mismatch(self):
        with self.assertRaises(ForemanError) as ctx:
            capabilities_mod.assert_repo_requirements(["ports"], set(), "local")
        self.assertIn("required_capabilities", str(ctx.exception))


class ComposedGate(unittest.TestCase):
    def cfg(self) -> Config:
        return Config(
            verify={
                "default": ["task", "verify"],
                "docker": ["task", "verify:docker"],
                "ports": ["task", "e2e"],
            }
        )

    def test_composition_skips_what_the_environment_lacks(self):
        # Spec scenario: local, max_parallel 3 → default + docker run, ports
        # is skipped and Actions remains the authority for it.
        commands = gate.compose(self.cfg(), {"docker"})
        self.assertEqual(commands, [["task", "verify"], ["task", "verify:docker"]])

    def test_composition_is_baseline_first_then_declaration_order(self):
        commands = gate.compose(self.cfg(), {"ports", "docker"})
        self.assertEqual(
            commands,
            [["task", "verify"], ["task", "verify:docker"], ["task", "e2e"]],
        )

    def test_bare_repo_composes_just_the_baseline(self):
        commands = gate.compose(Config(), {"docker"})
        self.assertEqual(commands, [["task", "verify"]])

    def test_describe_is_joined(self):
        self.assertEqual(
            gate.describe([["task", "verify"], ["task", "e2e"]]),
            "task verify && task e2e",
        )


class GateExecution(unittest.TestCase):
    def test_stops_at_first_failure_and_names_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ok, _tail, failed = verify.run_gate(
                [["true"], ["false"], ["true"]], root, root
            )
            self.assertFalse(ok)
            self.assertEqual(failed, ["false"])
            self.assertTrue((root / "verify.log").exists())

    def test_all_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ok, _tail, failed = verify.run_gate([["true"], ["true"]], root, root)
            self.assertTrue(ok)
            self.assertIsNone(failed)


if __name__ == "__main__":
    unittest.main()
