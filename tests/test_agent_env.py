"""The agent-environment allowlist (#13): what a unit is handed, and what it
is deliberately not. This asserts the ENV the agent receives, never that the
write token is unreachable on the box — under local it is reachable by
design (D1/D3), and a green test claiming otherwise would be worse than no
test."""

from __future__ import annotations

import os
import unittest

from foreman import backend as backend_mod
from foreman.config import Config
from foreman.util import ForemanError


class AgentEnvAllowlist(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_allowlist_shape(self):
        os.environ.update(
            {
                "PATH": "/usr/bin",
                "HOME": "/home/bot",
                "LANG": "C.UTF-8",
                "FOREMAN_AGENT_GH_TOKEN": "read-token",
                "CLAUDE_CODE_OAUTH_TOKEN": "oauth",
                # Must never reach the agent env:
                "GH_TOKEN": "WRITE-token",
                "GITHUB_TOKEN": "WRITE-token-2",
                "ANTHROPIC_API_KEY": "api-key",
                "ANTHROPIC_AUTH_TOKEN": "auth-token",
                "SOME_RANDOM_SECRET": "nope",
                # An operator-only FOREMAN_* control must NOT leak by prefix —
                # the allowlist is explicit, not a sweep.
                "FOREMAN_SANDBOXED": "1",
                "FOREMAN_SECRET_KNOB": "nope",
            }
        )
        env = backend_mod.agent_env(Config())
        self.assertEqual(env["GH_TOKEN"], "read-token")  # read token AS GH_TOKEN
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertEqual(env["HOME"], "/home/bot")
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "oauth")
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertNotIn("SOME_RANDOM_SECRET", env)
        # No blanket FOREMAN_* forwarding: operator-only vars stay out.
        self.assertNotIn("FOREMAN_SANDBOXED", env)
        self.assertNotIn("FOREMAN_SECRET_KNOB", env)
        # The raw provisioning var is consumed, not passed through.
        self.assertNotIn("FOREMAN_AGENT_GH_TOKEN", env)
        self.assertNotEqual(env["GH_TOKEN"], "WRITE-token")

    def test_missing_read_token_refuses_dispatch(self):
        os.environ.pop("FOREMAN_AGENT_GH_TOKEN", None)
        with self.assertRaises(ForemanError) as ctx:
            backend_mod.agent_env(Config())
        self.assertIn("FOREMAN_AGENT_GH_TOKEN", str(ctx.exception))

    def test_api_billing_key_flows_as_foreman_var(self):
        os.environ.update(
            {
                "PATH": "/usr/bin",
                "FOREMAN_AGENT_GH_TOKEN": "read-token",
                "FOREMAN_ANTHROPIC_API_KEY": "unit-budget-key",
            }
        )
        env = backend_mod.agent_env(Config(billing="api"))
        self.assertEqual(env["FOREMAN_ANTHROPIC_API_KEY"], "unit-budget-key")


if __name__ == "__main__":
    unittest.main()
