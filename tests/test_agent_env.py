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

    def test_readonly_flag_forwards_when_set(self):
        # vet's read-only mode must actually reach the adapter — the total
        # allowlist otherwise silently dropped it and vet ran writable.
        os.environ.update(
            {"PATH": "/usr/bin", "FOREMAN_AGENT_GH_TOKEN": "t", "FOREMAN_READONLY": "1"}
        )
        env = backend_mod.agent_env(Config())
        self.assertEqual(env.get("FOREMAN_READONLY"), "1")

    def test_readonly_flag_absent_when_unset(self):
        os.environ.pop("FOREMAN_READONLY", None)
        os.environ.update({"PATH": "/usr/bin", "FOREMAN_AGENT_GH_TOKEN": "t"})
        env = backend_mod.agent_env(Config())
        self.assertNotIn("FOREMAN_READONLY", env)

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
                "DEEPSEEK_API_KEY": "deepseek-key",
                "MOONSHOT_API_KEY": "moonshot-key",
                "ZAI_API_KEY": "zai-key",
                "OPENAI_API_KEY": "openai-key",
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
        self.assertNotIn("DEEPSEEK_API_KEY", env)
        self.assertNotIn("MOONSHOT_API_KEY", env)
        self.assertNotIn("ZAI_API_KEY", env)
        self.assertNotIn("OPENAI_API_KEY", env)
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

    def test_deepseek_key_flows_only_as_foreman_var(self):
        os.environ.update(
            {
                "PATH": "/usr/bin",
                "FOREMAN_AGENT_GH_TOKEN": "read-token",
                "FOREMAN_DEEPSEEK_API_KEY": "deepseek-unit-key",
                "DEEPSEEK_API_KEY": "raw-key-must-not-flow",
            }
        )
        env = backend_mod.agent_env(Config(backend="claude-code-deepseek"))
        self.assertEqual(env["FOREMAN_DEEPSEEK_API_KEY"], "deepseek-unit-key")
        self.assertNotIn("DEEPSEEK_API_KEY", env)

    def test_kimi_key_flows_only_as_foreman_var(self):
        os.environ.update(
            {
                "PATH": "/usr/bin",
                "FOREMAN_AGENT_GH_TOKEN": "read-token",
                "FOREMAN_KIMI_API_KEY": "kimi-unit-key",
                "MOONSHOT_API_KEY": "raw-key-must-not-flow",
            }
        )
        env = backend_mod.agent_env(Config(backend="claude-code-kimi"))
        self.assertEqual(env["FOREMAN_KIMI_API_KEY"], "kimi-unit-key")
        self.assertNotIn("MOONSHOT_API_KEY", env)

    def test_glm_key_flows_only_as_foreman_var(self):
        os.environ.update(
            {
                "PATH": "/usr/bin",
                "FOREMAN_AGENT_GH_TOKEN": "read-token",
                "FOREMAN_GLM_API_KEY": "glm-unit-key",
                "ZAI_API_KEY": "raw-key-must-not-flow",
            }
        )
        env = backend_mod.agent_env(Config(backend="claude-code-glm"))
        self.assertEqual(env["FOREMAN_GLM_API_KEY"], "glm-unit-key")
        self.assertNotIn("ZAI_API_KEY", env)

    def test_openai_key_flows_only_as_foreman_var(self):
        os.environ.update(
            {
                "PATH": "/usr/bin",
                "FOREMAN_AGENT_GH_TOKEN": "read-token",
                "FOREMAN_OPENAI_API_KEY": "openai-unit-key",
                "OPENAI_API_KEY": "raw-key-must-not-flow",
            }
        )
        env = backend_mod.agent_env(Config(backend="codex-cli", billing="api"))
        self.assertEqual(env["FOREMAN_OPENAI_API_KEY"], "openai-unit-key")
        self.assertNotIn("OPENAI_API_KEY", env)

    def test_codex_home_forwards_when_set(self):
        # A relocated Codex home (login + config.toml) must reach the adapter,
        # else codex-cli falls back to $HOME/.codex and loses the login/config.
        os.environ.update(
            {
                "PATH": "/usr/bin",
                "FOREMAN_AGENT_GH_TOKEN": "read-token",
                "CODEX_HOME": "/opt/codex-home",
            }
        )
        env = backend_mod.agent_env(Config(backend="codex-cli"))
        self.assertEqual(env["CODEX_HOME"], "/opt/codex-home")

    def test_codex_model_flows_as_runner_config_var(self):
        # The codex-cli model knob is non-secret runner configuration; it must
        # reach the adapter but only via its explicit FOREMAN_* name.
        os.environ.update(
            {
                "PATH": "/usr/bin",
                "FOREMAN_AGENT_GH_TOKEN": "read-token",
                "FOREMAN_CODEX_MODEL": "gpt-5-codex",
            }
        )
        env = backend_mod.agent_env(Config(backend="codex-cli"))
        self.assertEqual(env["FOREMAN_CODEX_MODEL"], "gpt-5-codex")


if __name__ == "__main__":
    unittest.main()
