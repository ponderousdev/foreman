"""Executable backend-adapter contracts."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from foreman import backend as backend_mod
from foreman.config import Config
from foreman.runner import Handle, UnitSpec
from foreman.util import ForemanError
from tests.mock_runner import MockRunner


class DeepSeekAdapter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.capture = self.root / "claude.capture"
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("work\n", encoding="utf-8")
        self.result = self.root / "result.json"
        self.session = self.root / "session"
        self.log = self.root / "agent.log"
        self.adapter = backend_mod.adapter_path("claude-code-deepseek")
        fake = self.bin / "claude"
        fake.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                {
                    printf 'ARGS'
                    for arg in "$@"; do printf '\\t%s' "$arg"; done
                    printf '\\n'
                    printf 'BASE=%s\\n' "${ANTHROPIC_BASE_URL:-}"
                    printf 'MODEL=%s\\n' "${ANTHROPIC_MODEL:-}"
                    printf 'OPUS=%s\\n' "${ANTHROPIC_DEFAULT_OPUS_MODEL:-}"
                    printf 'SONNET=%s\\n' "${ANTHROPIC_DEFAULT_SONNET_MODEL:-}"
                    printf 'HAIKU=%s\\n' "${ANTHROPIC_DEFAULT_HAIKU_MODEL:-}"
                    printf 'FABLE=%s\\n' "${ANTHROPIC_DEFAULT_FABLE_MODEL:-}"
                    printf 'SUBAGENT=%s\\n' "${CLAUDE_CODE_SUBAGENT_MODEL:-}"
                    printf 'EFFORT=%s\\n' "${CLAUDE_CODE_EFFORT_LEVEL:-}"
                    printf 'MCP=%s\\n' "${ENABLE_CLAUDEAI_MCP_SERVERS:-}"
                    [ "${ANTHROPIC_AUTH_TOKEN:-}" = 'test-deepseek-key' ] && echo 'AUTH_MATCH=yes' || echo 'AUTH_MATCH=no'
                    [ "${FOREMAN_DEEPSEEK_API_KEY+x}" = x ] && echo 'FOREMAN_KEY_PRESENT=yes' || echo 'FOREMAN_KEY_PRESENT=no'
                    [ "${DEEPSEEK_API_KEY+x}" = x ] && echo 'RAW_KEY_PRESENT=yes' || echo 'RAW_KEY_PRESENT=no'
                    [ "${ANTHROPIC_API_KEY+x}" = x ] && echo 'ANTHROPIC_KEY_PRESENT=yes' || echo 'ANTHROPIC_KEY_PRESENT=no'
                    [ "${CLAUDE_CODE_OAUTH_TOKEN+x}" = x ] && echo 'OAUTH_PRESENT=yes' || echo 'OAUTH_PRESENT=no'
                } >"$FAKE_CLAUDE_CAPTURE"
                printf '{"type":"system","session_id":"deepseek-session"}\\n'
                printf '{"type":"result","total_cost_usd":99.99}\\n'
                if [ -n "${FAKE_CLAUDE_ERROR:-}" ]; then
                    printf 'provider error: %s\\n' "$FAKE_CLAUDE_ERROR"
                fi
                exit "${FAKE_CLAUDE_EXIT:-0}"
                """
            ),
            encoding="utf-8",
        )
        fake.chmod(0o755)

    def env(self, **changes: str) -> dict[str, str]:
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FOREMAN_PROMPT_FILE": str(self.prompt),
            "FOREMAN_RESULT_FILE": str(self.result),
            "FOREMAN_SESSION_FILE": str(self.session),
            "FOREMAN_LOG_FILE": str(self.log),
            "FOREMAN_PERMISSION_MODE": "acceptEdits",
            "FOREMAN_BILLING": "api",
            "FOREMAN_MAX_TURNS": "5",
            "FOREMAN_DEEPSEEK_API_KEY": "test-deepseek-key",
            "FOREMAN_ANTHROPIC_API_KEY": "competing-anthropic-key",
            "DEEPSEEK_API_KEY": "raw-deepseek-key",
            "ANTHROPIC_API_KEY": "raw-anthropic-key",
            "CLAUDE_CODE_OAUTH_TOKEN": "competing-oauth-token",
            "FAKE_CLAUDE_CAPTURE": str(self.capture),
        }
        env.update(changes)
        return env

    def run_adapter(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.adapter), *args],
            cwd=self.root,
            env=env or self.env(),
            text=True,
            capture_output=True,
            check=False,
        )

    def capture_lines(self) -> dict[str, str]:
        lines = self.capture.read_text(encoding="utf-8").splitlines()
        return dict(line.split("=", 1) for line in lines[1:])

    def test_capabilities_are_exact_and_do_not_require_credentials(self):
        proc = self.run_adapter("capabilities", env={"PATH": os.environ["PATH"]})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip().split(), ["resume", "attach"])

    def test_run_hardwires_deepseek_family_and_strips_competing_credentials(self):
        proc = self.run_adapter("run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        capture = self.capture_lines()
        self.assertEqual(capture["BASE"], "https://api.deepseek.com/anthropic")
        self.assertEqual(capture["MODEL"], "deepseek-v4-pro[1m]")
        self.assertEqual(capture["OPUS"], "deepseek-v4-pro[1m]")
        self.assertEqual(capture["SONNET"], "deepseek-v4-pro[1m]")
        self.assertEqual(capture["HAIKU"], "deepseek-v4-flash")
        self.assertEqual(capture["FABLE"], "deepseek-v4-pro[1m]")
        self.assertEqual(capture["SUBAGENT"], "deepseek-v4-flash")
        self.assertEqual(capture["EFFORT"], "max")
        self.assertEqual(capture["MCP"], "false")
        self.assertEqual(capture["AUTH_MATCH"], "yes")
        self.assertEqual(capture["FOREMAN_KEY_PRESENT"], "no")
        self.assertEqual(capture["RAW_KEY_PRESENT"], "no")
        self.assertEqual(capture["ANTHROPIC_KEY_PRESENT"], "no")
        self.assertEqual(capture["OAUTH_PRESENT"], "no")
        args = self.capture.read_text(encoding="utf-8").splitlines()[0].split("\t")[1:]
        self.assertEqual(
            args,
            [
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--permission-mode",
                "acceptEdits",
                "--add-dir",
                str(self.root),
                "--max-turns",
                "5",
            ],
        )
        self.assertEqual(
            self.session.read_text(encoding="utf-8"), "SESSION_REF=deepseek-session\n"
        )
        self.assertNotIn("COST_USD", self.session.read_text(encoding="utf-8"))
        self.assertIn(
            '"session_id":"deepseek-session"', self.log.read_text(encoding="utf-8")
        )

    def test_resume_passes_the_session_ref(self):
        proc = self.run_adapter("resume", "prior-session")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = self.capture.read_text(encoding="utf-8").splitlines()[0].split("\t")[1:]
        self.assertEqual(args[-2:], ["--resume", "prior-session"])

    def test_attach_uses_provider_environment_for_interactive_resume(self):
        self.session.write_text("SESSION_REF=prior-session\n", encoding="utf-8")
        proc = self.run_adapter("attach", "--session-file", str(self.session))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = self.capture.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0].split("\t")[1:], ["--resume", "prior-session"])
        capture = self.capture_lines()
        self.assertEqual(capture["BASE"], "https://api.deepseek.com/anthropic")
        self.assertEqual(capture["AUTH_MATCH"], "yes")
        self.assertEqual(capture["FOREMAN_KEY_PRESENT"], "no")

    def test_missing_key_and_subscription_billing_fail_closed_without_secret_echo(self):
        missing = self.env()
        missing.pop("FOREMAN_DEEPSEEK_API_KEY")
        proc = self.run_adapter("run", env=missing)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("FOREMAN_DEEPSEEK_API_KEY", proc.stderr)

        proc = self.run_adapter("run", env=self.env(FOREMAN_BILLING="subscription"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("requires billing=api", proc.stderr)
        self.assertNotIn("test-deepseek-key", proc.stderr)

    def test_provider_failure_preserves_log_and_exit_status(self):
        proc = self.run_adapter(
            "run",
            env=self.env(FAKE_CLAUDE_EXIT="7", FAKE_CLAUDE_ERROR="overloaded"),
        )
        self.assertEqual(proc.returncode, 7)
        self.assertIn(
            "provider error: overloaded", self.log.read_text(encoding="utf-8")
        )

    def test_shared_contract_keeps_direct_claude_capabilities_and_cost(self):
        self.adapter = backend_mod.adapter_path("claude")
        capabilities = self.run_adapter(
            "capabilities", env={"PATH": os.environ["PATH"]}
        )
        self.assertEqual(capabilities.returncode, 0, capabilities.stderr)
        self.assertEqual(set(capabilities.stdout.split()), {"resume", "cost", "attach"})

        proc = self.run_adapter("run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        capture = self.capture_lines()
        self.assertEqual(capture["FOREMAN_KEY_PRESENT"], "no")
        self.assertEqual(capture["OAUTH_PRESENT"], "no")
        self.assertEqual(capture["ANTHROPIC_KEY_PRESENT"], "yes")
        self.assertIn("COST_USD=99.99", self.session.read_text(encoding="utf-8"))

    def test_direct_api_billing_still_requires_its_own_key(self):
        self.adapter = backend_mod.adapter_path("claude")
        env = self.env()
        env.pop("FOREMAN_ANTHROPIC_API_KEY")
        proc = self.run_adapter("run", env=env)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("FOREMAN_ANTHROPIC_API_KEY", proc.stderr)


class KimiAdapter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.capture = self.root / "claude.capture"
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("work\n", encoding="utf-8")
        self.result = self.root / "result.json"
        self.session = self.root / "session"
        self.log = self.root / "agent.log"
        self.adapter = backend_mod.adapter_path("claude-code-kimi")
        fake = self.bin / "claude"
        fake.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                {
                    printf 'ARGS'
                    for arg in "$@"; do printf '\\t%s' "$arg"; done
                    printf '\\n'
                    printf 'BASE=%s\\n' "${ANTHROPIC_BASE_URL:-}"
                    printf 'MODEL=%s\\n' "${ANTHROPIC_MODEL:-}"
                    printf 'OPUS=%s\\n' "${ANTHROPIC_DEFAULT_OPUS_MODEL:-}"
                    printf 'SONNET=%s\\n' "${ANTHROPIC_DEFAULT_SONNET_MODEL:-}"
                    printf 'HAIKU=%s\\n' "${ANTHROPIC_DEFAULT_HAIKU_MODEL:-}"
                    printf 'FABLE=%s\\n' "${ANTHROPIC_DEFAULT_FABLE_MODEL:-}"
                    printf 'SUBAGENT=%s\\n' "${CLAUDE_CODE_SUBAGENT_MODEL:-}"
                    printf 'EFFORT=%s\\n' "${CLAUDE_CODE_EFFORT_LEVEL:-}"
                    printf 'MCP=%s\\n' "${ENABLE_CLAUDEAI_MCP_SERVERS:-}"
                    [ "${ANTHROPIC_AUTH_TOKEN:-}" = 'test-kimi-key' ] && echo 'AUTH_MATCH=yes' || echo 'AUTH_MATCH=no'
                    [ "${FOREMAN_KIMI_API_KEY+x}" = x ] && echo 'FOREMAN_KEY_PRESENT=yes' || echo 'FOREMAN_KEY_PRESENT=no'
                    [ "${MOONSHOT_API_KEY+x}" = x ] && echo 'RAW_KEY_PRESENT=yes' || echo 'RAW_KEY_PRESENT=no'
                    [ "${ANTHROPIC_API_KEY+x}" = x ] && echo 'ANTHROPIC_KEY_PRESENT=yes' || echo 'ANTHROPIC_KEY_PRESENT=no'
                    [ "${CLAUDE_CODE_OAUTH_TOKEN+x}" = x ] && echo 'OAUTH_PRESENT=yes' || echo 'OAUTH_PRESENT=no'
                } >"$FAKE_CLAUDE_CAPTURE"
                printf '{"type":"system","session_id":"kimi-session"}\\n'
                printf '{"type":"result","total_cost_usd":99.99}\\n'
                if [ -n "${FAKE_CLAUDE_ERROR:-}" ]; then
                    printf 'provider error: %s\\n' "$FAKE_CLAUDE_ERROR"
                fi
                exit "${FAKE_CLAUDE_EXIT:-0}"
                """
            ),
            encoding="utf-8",
        )
        fake.chmod(0o755)

    def env(self, **changes: str) -> dict[str, str]:
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FOREMAN_PROMPT_FILE": str(self.prompt),
            "FOREMAN_RESULT_FILE": str(self.result),
            "FOREMAN_SESSION_FILE": str(self.session),
            "FOREMAN_LOG_FILE": str(self.log),
            "FOREMAN_PERMISSION_MODE": "acceptEdits",
            "FOREMAN_BILLING": "api",
            "FOREMAN_MAX_TURNS": "5",
            "FOREMAN_KIMI_API_KEY": "test-kimi-key",
            "FOREMAN_ANTHROPIC_API_KEY": "competing-anthropic-key",
            "MOONSHOT_API_KEY": "raw-kimi-key",
            "ANTHROPIC_API_KEY": "raw-anthropic-key",
            "CLAUDE_CODE_OAUTH_TOKEN": "competing-oauth-token",
            "FAKE_CLAUDE_CAPTURE": str(self.capture),
        }
        env.update(changes)
        return env

    def run_adapter(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.adapter), *args],
            cwd=self.root,
            env=env or self.env(),
            text=True,
            capture_output=True,
            check=False,
        )

    def capture_lines(self) -> dict[str, str]:
        lines = self.capture.read_text(encoding="utf-8").splitlines()
        return dict(line.split("=", 1) for line in lines[1:])

    def test_capabilities_are_exact_and_do_not_require_credentials(self):
        proc = self.run_adapter("capabilities", env={"PATH": os.environ["PATH"]})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip().split(), ["resume", "attach"])

    def test_run_hardwires_kimi_family_and_strips_competing_credentials(self):
        proc = self.run_adapter("run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        capture = self.capture_lines()
        self.assertEqual(capture["BASE"], "https://api.moonshot.ai/anthropic")
        self.assertEqual(capture["MODEL"], "kimi-k3[1m]")
        self.assertEqual(capture["OPUS"], "kimi-k3[1m]")
        self.assertEqual(capture["SONNET"], "kimi-k3[1m]")
        self.assertEqual(capture["HAIKU"], "kimi-k2.7-code-highspeed")
        self.assertEqual(capture["FABLE"], "kimi-k3[1m]")
        self.assertEqual(capture["SUBAGENT"], "kimi-k2.7-code-highspeed")
        self.assertEqual(capture["EFFORT"], "max")
        self.assertEqual(capture["MCP"], "false")
        self.assertEqual(capture["AUTH_MATCH"], "yes")
        self.assertEqual(capture["FOREMAN_KEY_PRESENT"], "no")
        self.assertEqual(capture["RAW_KEY_PRESENT"], "no")
        self.assertEqual(capture["ANTHROPIC_KEY_PRESENT"], "no")
        self.assertEqual(capture["OAUTH_PRESENT"], "no")
        args = self.capture.read_text(encoding="utf-8").splitlines()[0].split("\t")[1:]
        self.assertEqual(
            args,
            [
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--permission-mode",
                "acceptEdits",
                "--add-dir",
                str(self.root),
                "--max-turns",
                "5",
            ],
        )
        self.assertEqual(
            self.session.read_text(encoding="utf-8"), "SESSION_REF=kimi-session\n"
        )
        self.assertNotIn("COST_USD", self.session.read_text(encoding="utf-8"))
        self.assertIn(
            '"session_id":"kimi-session"', self.log.read_text(encoding="utf-8")
        )

    def test_resume_passes_the_session_ref(self):
        proc = self.run_adapter("resume", "prior-session")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = self.capture.read_text(encoding="utf-8").splitlines()[0].split("\t")[1:]
        self.assertEqual(args[-2:], ["--resume", "prior-session"])

    def test_attach_uses_provider_environment_for_interactive_resume(self):
        self.session.write_text("SESSION_REF=prior-session\n", encoding="utf-8")
        proc = self.run_adapter("attach", "--session-file", str(self.session))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = self.capture.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0].split("\t")[1:], ["--resume", "prior-session"])
        capture = self.capture_lines()
        self.assertEqual(capture["BASE"], "https://api.moonshot.ai/anthropic")
        self.assertEqual(capture["AUTH_MATCH"], "yes")
        self.assertEqual(capture["FOREMAN_KEY_PRESENT"], "no")

    def test_missing_key_and_subscription_billing_fail_closed_without_secret_echo(self):
        missing = self.env()
        missing.pop("FOREMAN_KIMI_API_KEY")
        proc = self.run_adapter("run", env=missing)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("FOREMAN_KIMI_API_KEY", proc.stderr)

        proc = self.run_adapter("run", env=self.env(FOREMAN_BILLING="subscription"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("requires billing=api", proc.stderr)
        self.assertNotIn("test-kimi-key", proc.stderr)

    def test_provider_failure_preserves_log_and_exit_status(self):
        proc = self.run_adapter(
            "run",
            env=self.env(FAKE_CLAUDE_EXIT="7", FAKE_CLAUDE_ERROR="overloaded"),
        )
        self.assertEqual(proc.returncode, 7)
        self.assertIn(
            "provider error: overloaded", self.log.read_text(encoding="utf-8")
        )


class GLMAdapter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.capture = self.root / "claude.capture"
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("work\n", encoding="utf-8")
        self.result = self.root / "result.json"
        self.session = self.root / "session"
        self.log = self.root / "agent.log"
        self.adapter = backend_mod.adapter_path("claude-code-glm")
        fake = self.bin / "claude"
        fake.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                {
                    printf 'ARGS'
                    for arg in "$@"; do printf '\\t%s' "$arg"; done
                    printf '\\n'
                    printf 'BASE=%s\\n' "${ANTHROPIC_BASE_URL:-}"
                    printf 'MODEL=%s\\n' "${ANTHROPIC_MODEL:-}"
                    printf 'OPUS=%s\\n' "${ANTHROPIC_DEFAULT_OPUS_MODEL:-}"
                    printf 'SONNET=%s\\n' "${ANTHROPIC_DEFAULT_SONNET_MODEL:-}"
                    printf 'HAIKU=%s\\n' "${ANTHROPIC_DEFAULT_HAIKU_MODEL:-}"
                    printf 'FABLE=%s\\n' "${ANTHROPIC_DEFAULT_FABLE_MODEL:-}"
                    printf 'SUBAGENT=%s\\n' "${CLAUDE_CODE_SUBAGENT_MODEL:-}"
                    printf 'EFFORT=%s\\n' "${CLAUDE_CODE_EFFORT_LEVEL:-}"
                    printf 'MCP=%s\\n' "${ENABLE_CLAUDEAI_MCP_SERVERS:-}"
                    printf 'TIMEOUT=%s\\n' "${API_TIMEOUT_MS:-}"
                    [ "${ANTHROPIC_AUTH_TOKEN:-}" = 'test-glm-key' ] && echo 'AUTH_MATCH=yes' || echo 'AUTH_MATCH=no'
                    [ "${FOREMAN_GLM_API_KEY+x}" = x ] && echo 'FOREMAN_KEY_PRESENT=yes' || echo 'FOREMAN_KEY_PRESENT=no'
                    [ "${ZAI_API_KEY+x}" = x ] && echo 'RAW_KEY_PRESENT=yes' || echo 'RAW_KEY_PRESENT=no'
                    [ "${ANTHROPIC_API_KEY+x}" = x ] && echo 'ANTHROPIC_KEY_PRESENT=yes' || echo 'ANTHROPIC_KEY_PRESENT=no'
                    [ "${CLAUDE_CODE_OAUTH_TOKEN+x}" = x ] && echo 'OAUTH_PRESENT=yes' || echo 'OAUTH_PRESENT=no'
                } >"$FAKE_CLAUDE_CAPTURE"
                printf '{"type":"system","session_id":"glm-session"}\\n'
                printf '{"type":"result","total_cost_usd":99.99}\\n'
                if [ -n "${FAKE_CLAUDE_ERROR:-}" ]; then
                    printf 'provider error: %s\\n' "$FAKE_CLAUDE_ERROR"
                fi
                exit "${FAKE_CLAUDE_EXIT:-0}"
                """
            ),
            encoding="utf-8",
        )
        fake.chmod(0o755)

    def env(self, **changes: str) -> dict[str, str]:
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FOREMAN_PROMPT_FILE": str(self.prompt),
            "FOREMAN_RESULT_FILE": str(self.result),
            "FOREMAN_SESSION_FILE": str(self.session),
            "FOREMAN_LOG_FILE": str(self.log),
            "FOREMAN_PERMISSION_MODE": "acceptEdits",
            "FOREMAN_BILLING": "api",
            "FOREMAN_MAX_TURNS": "5",
            "FOREMAN_GLM_API_KEY": "test-glm-key",
            "FOREMAN_ANTHROPIC_API_KEY": "competing-anthropic-key",
            "ZAI_API_KEY": "raw-glm-key",
            "ANTHROPIC_API_KEY": "raw-anthropic-key",
            "CLAUDE_CODE_OAUTH_TOKEN": "competing-oauth-token",
            "FAKE_CLAUDE_CAPTURE": str(self.capture),
        }
        env.update(changes)
        return env

    def run_adapter(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.adapter), *args],
            cwd=self.root,
            env=env or self.env(),
            text=True,
            capture_output=True,
            check=False,
        )

    def capture_lines(self) -> dict[str, str]:
        lines = self.capture.read_text(encoding="utf-8").splitlines()
        return dict(line.split("=", 1) for line in lines[1:])

    def test_capabilities_are_exact_and_do_not_require_credentials(self):
        proc = self.run_adapter("capabilities", env={"PATH": os.environ["PATH"]})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip().split(), ["resume", "attach"])

    def test_run_hardwires_glm_family_and_strips_competing_credentials(self):
        proc = self.run_adapter("run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        capture = self.capture_lines()
        self.assertEqual(capture["BASE"], "https://api.z.ai/api/anthropic")
        self.assertEqual(capture["MODEL"], "glm-5.2[1m]")
        self.assertEqual(capture["OPUS"], "glm-5.2[1m]")
        self.assertEqual(capture["SONNET"], "glm-5.2[1m]")
        self.assertEqual(capture["HAIKU"], "glm-4.7")
        self.assertEqual(capture["FABLE"], "glm-5.2[1m]")
        self.assertEqual(capture["SUBAGENT"], "glm-4.7")
        self.assertEqual(capture["EFFORT"], "max")
        self.assertEqual(capture["MCP"], "false")
        self.assertEqual(capture["TIMEOUT"], "3000000")
        self.assertEqual(capture["AUTH_MATCH"], "yes")
        self.assertEqual(capture["FOREMAN_KEY_PRESENT"], "no")
        self.assertEqual(capture["RAW_KEY_PRESENT"], "no")
        self.assertEqual(capture["ANTHROPIC_KEY_PRESENT"], "no")
        self.assertEqual(capture["OAUTH_PRESENT"], "no")
        args = self.capture.read_text(encoding="utf-8").splitlines()[0].split("\t")[1:]
        self.assertEqual(
            args,
            [
                "-p",
                "--output-format",
                "stream-json",
                "--verbose",
                "--permission-mode",
                "acceptEdits",
                "--add-dir",
                str(self.root),
                "--max-turns",
                "5",
            ],
        )
        self.assertEqual(
            self.session.read_text(encoding="utf-8"), "SESSION_REF=glm-session\n"
        )
        self.assertNotIn("COST_USD", self.session.read_text(encoding="utf-8"))
        self.assertIn(
            '"session_id":"glm-session"', self.log.read_text(encoding="utf-8")
        )

    def test_resume_passes_the_session_ref(self):
        proc = self.run_adapter("resume", "prior-session")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = self.capture.read_text(encoding="utf-8").splitlines()[0].split("\t")[1:]
        self.assertEqual(args[-2:], ["--resume", "prior-session"])

    def test_attach_uses_provider_environment_for_interactive_resume(self):
        self.session.write_text("SESSION_REF=prior-session\n", encoding="utf-8")
        proc = self.run_adapter("attach", "--session-file", str(self.session))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = self.capture.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0].split("\t")[1:], ["--resume", "prior-session"])
        capture = self.capture_lines()
        self.assertEqual(capture["BASE"], "https://api.z.ai/api/anthropic")
        self.assertEqual(capture["AUTH_MATCH"], "yes")
        self.assertEqual(capture["FOREMAN_KEY_PRESENT"], "no")

    def test_missing_key_and_subscription_billing_fail_closed_without_secret_echo(self):
        missing = self.env()
        missing.pop("FOREMAN_GLM_API_KEY")
        proc = self.run_adapter("run", env=missing)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("FOREMAN_GLM_API_KEY", proc.stderr)

        proc = self.run_adapter("run", env=self.env(FOREMAN_BILLING="subscription"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("requires billing=api", proc.stderr)
        self.assertNotIn("test-glm-key", proc.stderr)

    def test_provider_failure_preserves_log_and_exit_status(self):
        proc = self.run_adapter(
            "run",
            env=self.env(FAKE_CLAUDE_EXIT="7", FAKE_CLAUDE_ERROR="overloaded"),
        )
        self.assertEqual(proc.returncode, 7)
        self.assertIn(
            "provider error: overloaded", self.log.read_text(encoding="utf-8")
        )

    def test_client_timeout_stays_above_foreman_deadline(self):
        # The Anthropic client's per-request timeout must exceed foreman's
        # whole-unit budget so the supervisor (backend.py) owns the deadline,
        # never the client. Derived as (FOREMAN_TIMEOUT_MIN + 10 min) in ms.
        self.run_adapter("run", env=self.env(FOREMAN_TIMEOUT_MIN="90"))
        self.assertEqual(self.capture_lines()["TIMEOUT"], str((90 + 10) * 60 * 1000))
        # Uncapped units (timeout_min = 0) fall back to a generous fixed value.
        self.run_adapter("run", env=self.env(FOREMAN_TIMEOUT_MIN="0"))
        self.assertEqual(self.capture_lines()["TIMEOUT"], "3000000")


class CodexCliAdapter(unittest.TestCase):
    """OpenAI Codex CLI adapter. Codex is a different harness from Claude Code
    (`codex exec`, `thread.started` events, `-c` config overrides), so this
    standalone adapter is exercised against a fake `codex` on PATH."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # A real repo so the adapter's `git rev-parse --git-common-dir` resolves
        # (the worktree Git-metadata writable root); adapter cwd is self.root.
        subprocess.run(
            ["git", "init", "-q", str(self.root)], check=True, capture_output=True
        )
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.capture = self.root / "codex.capture"
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("work\n", encoding="utf-8")
        self.result = self.root / "result.json"
        self.session = self.root / "session"
        self.log = self.root / "agent.log"
        # Codex thread ids are UUIDs; the adapter only persists a UUID SESSION_REF.
        self.sid = "019fe240-8ec9-7283-8060-acf56ec96411"
        self.adapter = backend_mod.adapter_path("codex-cli")
        fake = self.bin / "codex"
        fake.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env bash
                set -euo pipefail
                {
                    printf 'ARGS'
                    for arg in "$@"; do printf '\\t%s' "$arg"; done
                    printf '\\n'
                    [ "${OPENAI_API_KEY:-}" = 'test-openai-key' ] && echo 'OPENAI_MATCH=yes' || echo 'OPENAI_MATCH=no'
                    [ "${OPENAI_API_KEY+x}" = x ] && echo 'OPENAI_PRESENT=yes' || echo 'OPENAI_PRESENT=no'
                    [ "${FOREMAN_OPENAI_API_KEY+x}" = x ] && echo 'FOREMAN_KEY_PRESENT=yes' || echo 'FOREMAN_KEY_PRESENT=no'
                    [ "${ANTHROPIC_API_KEY+x}" = x ] && echo 'ANTHROPIC_KEY_PRESENT=yes' || echo 'ANTHROPIC_KEY_PRESENT=no'
                    [ "${CLAUDE_CODE_OAUTH_TOKEN+x}" = x ] && echo 'OAUTH_PRESENT=yes' || echo 'OAUTH_PRESENT=no'
                    [ "${DEEPSEEK_API_KEY+x}" = x ] && echo 'DEEPSEEK_PRESENT=yes' || echo 'DEEPSEEK_PRESENT=no'
                } >"$FAKE_CODEX_CAPTURE"
                printf '{"type":"thread.started","thread_id":"%s"}\\n' "${FAKE_CODEX_THREAD_ID:-019fe240-8ec9-7283-8060-acf56ec96411}"
                printf '{"type":"turn.completed","usage":{"output_tokens":5}}\\n'
                if [ -n "${FAKE_CODEX_ERROR:-}" ]; then
                    printf 'codex error: %s\\n' "$FAKE_CODEX_ERROR"
                fi
                exit "${FAKE_CODEX_EXIT:-0}"
                """
            ),
            encoding="utf-8",
        )
        fake.chmod(0o755)

    def env(self, **changes: str) -> dict[str, str]:
        env = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FOREMAN_PROMPT_FILE": str(self.prompt),
            "FOREMAN_RESULT_FILE": str(self.result),
            "FOREMAN_SESSION_FILE": str(self.session),
            "FOREMAN_LOG_FILE": str(self.log),
            # Subscription is the only supported billing (ChatGPT login).
            "FOREMAN_BILLING": "subscription",
            # A stray provider key must be stripped, never reach Codex/its shell.
            "FOREMAN_OPENAI_API_KEY": "stray-openai-key",
            "OPENAI_API_KEY": "stray-raw-openai-key",
            # Competing provider credentials that must never reach Codex.
            "FOREMAN_ANTHROPIC_API_KEY": "competing-anthropic-key",
            "ANTHROPIC_API_KEY": "raw-anthropic-key",
            "CLAUDE_CODE_OAUTH_TOKEN": "competing-oauth-token",
            "DEEPSEEK_API_KEY": "raw-deepseek-key",
            "FAKE_CODEX_CAPTURE": str(self.capture),
        }
        env.update(changes)
        return env

    def run_adapter(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.adapter), *args],
            cwd=self.root,
            env=env or self.env(),
            text=True,
            capture_output=True,
            check=False,
        )

    def capture_args(self) -> list[str]:
        return self.capture.read_text(encoding="utf-8").splitlines()[0].split("\t")[1:]

    def capture_lines(self) -> dict[str, str]:
        lines = self.capture.read_text(encoding="utf-8").splitlines()
        return dict(line.split("=", 1) for line in lines[1:])

    def writable_roots_arg(self) -> str:
        for a in self.capture_args():
            if a.startswith("sandbox_workspace_write.writable_roots="):
                return a
        return ""

    def test_capabilities_are_exact_and_do_not_require_credentials(self):
        proc = self.run_adapter("capabilities", env={"PATH": os.environ["PATH"]})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip().split(), ["resume", "attach"])

    def test_run_uses_chatgpt_login_and_strips_all_provider_keys(self):
        proc = self.run_adapter("run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        capture = self.capture_lines()
        # Subscription uses the ChatGPT login; NO OpenAI key (stray or
        # provisioning) survives into Codex or, through it, the model's shell.
        self.assertEqual(capture["OPENAI_PRESENT"], "no")
        self.assertEqual(capture["FOREMAN_KEY_PRESENT"], "no")
        # No competing vendor credential survives into Codex's child process.
        self.assertEqual(capture["ANTHROPIC_KEY_PRESENT"], "no")
        self.assertEqual(capture["OAUTH_PRESENT"], "no")
        self.assertEqual(capture["DEEPSEEK_PRESENT"], "no")
        args = self.capture_args()
        self.assertIn("forced_login_method=chatgpt", args)
        self.assertIn("sandbox_mode=workspace-write", args)
        # Network is enabled so the agent can use gh / fetch deps / reach test
        # services — the runner, not Codex's sandbox, is the isolation boundary.
        self.assertIn("sandbox_workspace_write.network_access=true", args)
        # Writable roots grant the out-of-worktree result dir AND the worktree's
        # real Git metadata dir (so `git commit` works under workspace-write).
        roots = self.writable_roots_arg()
        self.assertIn(f'"{self.root}"', roots)
        self.assertIn("/.git", roots)
        # Headless run streams JSONL and reads the prompt from stdin via `-`.
        self.assertEqual(args[-4:], ["exec", "--json", "--skip-git-repo-check", "-"])
        self.assertEqual(
            self.session.read_text(encoding="utf-8"), f"SESSION_REF={self.sid}\n"
        )
        self.assertNotIn("COST_USD", self.session.read_text(encoding="utf-8"))
        self.assertIn(f'"thread_id":"{self.sid}"', self.log.read_text(encoding="utf-8"))

    def test_api_billing_fails_closed_to_protect_the_provider_key(self):
        # api billing is unsupported: an exported OPENAI_API_KEY would reach the
        # model's shell commands (verified on codex 0.147.0) with network on.
        proc = self.run_adapter("run", env=self.env(FOREMAN_BILLING="api"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("does not support billing=api", proc.stderr)
        # Fail closed WITHOUT echoing any secret material, before launching codex.
        self.assertNotIn("stray-openai-key", proc.stderr)
        self.assertFalse(self.capture.exists(), "codex must not launch under api")

    def test_model_is_runner_configuration_not_a_label(self):
        # Set → an explicit runner-config model flows to Codex as `-c model=`.
        self.run_adapter("run", env=self.env(FOREMAN_CODEX_MODEL="gpt-5-codex"))
        self.assertIn("model=gpt-5-codex", self.capture_args())
        # Unset → the adapter arms no model itself (Codex config decides); there
        # is no code path that reads an advisory suggest:* label.
        self.run_adapter("run")
        self.assertFalse(
            any(a.startswith("model=") for a in self.capture_args()),
            "adapter must not arm a model without explicit runner configuration",
        )

    def test_resume_passes_the_session_ref(self):
        proc = self.run_adapter("resume", self.sid)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = self.capture_args()
        self.assertEqual(
            args[-6:],
            ["exec", "resume", "--json", "--skip-git-repo-check", self.sid, "-"],
        )
        # The prompt still rides stdin via the trailing `-`.
        self.assertEqual(args[-1], "-")
        self.assertIn(f"SESSION_REF={self.sid}", self.session.read_text("utf-8"))

    def test_resume_requires_a_session_ref(self):
        proc = self.run_adapter("resume")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("resume requires a session ref", proc.stderr)

    def test_resume_rejects_option_shaped_session_ref(self):
        # A corrupted agent-writable ledger must not smuggle `--last` (or any
        # non-UUID) into Codex's positional parser and hijack another thread.
        for bad in ("--last", "../etc", "codex-session"):
            proc = self.run_adapter("resume", bad)
            self.assertNotEqual(proc.returncode, 0, f"{bad!r} should be rejected")
            self.assertIn("not a valid session id", proc.stderr)
            self.assertFalse(self.capture.exists(), "codex must not launch")

    def test_run_ignores_non_uuid_thread_id_from_stream(self):
        # Only a UUID thread id becomes a resumable SESSION_REF.
        proc = self.run_adapter("run", env=self.env(FAKE_CODEX_THREAD_ID="--last"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(
            self.session.exists() and self.session.read_text("utf-8").strip(),
            "a non-UUID thread id must never be persisted as a session ref",
        )

    def test_attach_uses_provider_environment_without_sandbox_roots(self):
        self.session.write_text(f"SESSION_REF={self.sid}\n", encoding="utf-8")
        proc = self.run_adapter("attach", "--session-file", str(self.session))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = self.capture_args()
        self.assertEqual(args[-2:], ["resume", self.sid])
        self.assertIn("forced_login_method=chatgpt", args)
        # Interactive attach takes no headless sandbox overrides — so it never
        # broadens the sandbox with a writable root for an unused result path.
        self.assertFalse(any(a.startswith("sandbox_") for a in args))
        self.assertEqual(self.capture_lines()["OPENAI_PRESENT"], "no")

    def test_attach_without_ref_starts_a_fresh_session(self):
        # No ref (unit died before emitting one) → a fresh interactive session,
        # never bare `codex resume` (which opens the global session picker).
        proc = self.run_adapter("attach")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = self.capture_args()
        self.assertNotIn("resume", args)
        self.assertIn("forced_login_method=chatgpt", args)

    def test_attach_rejects_option_shaped_ref_from_session_file(self):
        self.session.write_text("SESSION_REF=--last\n", encoding="utf-8")
        proc = self.run_adapter("attach", "--session-file", str(self.session))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not a valid session id", proc.stderr)

    def test_nonzero_max_turns_fails_closed(self):
        # Codex exec has no turn cap; refuse rather than silently ignore a bound.
        proc = self.run_adapter("run", env=self.env(FOREMAN_MAX_TURNS="5"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("max_turns", proc.stderr)
        self.assertFalse(self.capture.exists(), "codex must not launch")
        # max_turns=0 (uncapped, the default) runs normally.
        ok = self.run_adapter("run", env=self.env(FOREMAN_MAX_TURNS="0"))
        self.assertEqual(ok.returncode, 0, ok.stderr)

    def test_writable_roots_are_toml_escaped(self):
        # A result dir whose path contains a double quote must not break the
        # writable_roots TOML array.
        weird = self.root / 'a"b'
        weird.mkdir()
        env = self.env(FOREMAN_RESULT_FILE=str(weird / "result.json"))
        proc = self.run_adapter("run", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(r"a\"b", self.writable_roots_arg())

    def test_readonly_vet_uses_read_only_sandbox_and_no_json(self):
        proc = self.run_adapter("run", env=self.env(FOREMAN_READONLY="1"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        args = self.capture_args()
        self.assertIn("sandbox_mode=read-only", args)
        # Vet grants no writable root, enables no network, and streams no JSONL.
        self.assertFalse(any(a.startswith("sandbox_workspace_write") for a in args))
        self.assertNotIn("--json", args)
        self.assertEqual(args[-2:], ["--skip-git-repo-check", "-"])

    def test_provider_failure_preserves_log_and_exit_status(self):
        proc = self.run_adapter(
            "run",
            env=self.env(FAKE_CODEX_EXIT="7", FAKE_CODEX_ERROR="overloaded"),
        )
        self.assertEqual(proc.returncode, 7)
        self.assertIn("codex error: overloaded", self.log.read_text(encoding="utf-8"))

    def test_unknown_command_fails_closed(self):
        proc = self.run_adapter("frobnicate")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unknown command", proc.stderr)


class SessionLedgerIsolation(unittest.TestCase):
    def test_ledger_is_outside_the_agent_writable_run_dir(self):
        # Dispatch grants the agent write access to the run dir (the result
        # contract lives there); the adapter-owned ledger must sit outside it so
        # a unit cannot forge another unit's SESSION_REF.
        run_dir = Path("/x/.foreman/units/9")
        ledger = backend_mod.session_ledger(run_dir)
        self.assertEqual(ledger, Path("/x/.foreman/units/9.session"))
        self.assertNotEqual(ledger.parent, run_dir)
        self.assertFalse(
            str(ledger).startswith(str(run_dir) + "/"),
            "ledger must not live inside the agent-writable run dir",
        )


class BackendRunRecord(unittest.TestCase):
    def test_fresh_spawn_clears_stale_provider_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / ".foreman" / "units" / "9"
            run_dir.mkdir(parents=True)
            worktree = root / "worktree"
            worktree.mkdir()
            prompt = run_dir / "prompt.md"
            prompt.write_text("work\n", encoding="utf-8")
            # The ledger is a SIBLING of the run dir (outside agent-writable roots).
            session = backend_mod.session_ledger(run_dir)
            session.write_text("SESSION_REF=old-provider-session\n", encoding="utf-8")

            def new_session(spec: UnitSpec) -> int:
                with Path(spec.env["FOREMAN_SESSION_FILE"]).open(
                    "a", encoding="utf-8"
                ) as session_stream:
                    session_stream.write("SESSION_REF=new-provider-session\n")
                return 0

            with (
                mock.patch.object(backend_mod, "agent_env", return_value={}),
                mock.patch.object(backend_mod, "backend_cli_version", return_value=""),
            ):
                result = backend_mod.run_backend(
                    Config(),
                    root,
                    MockRunner(agent=new_session),
                    backend_mod.adapter_path("claude-code-deepseek"),
                    unit_number=9,
                    cwd=worktree,
                    unit_run_dir=run_dir,
                    prompt_file=prompt,
                    timeout_min=1,
                )
            self.assertEqual(result.session_ref, "new-provider-session")
            self.assertEqual(
                session.read_text(encoding="utf-8"),
                "SESSION_REF=new-provider-session\n",
            )

    def test_records_selected_adapter_instead_of_repository_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            spec = UnitSpec(
                unit=9,
                workdir=run_dir,
                run_dir=run_dir,
                env={},
                cmd=("adapter", "run"),
                timeout_s=60,
            )
            handle = Handle(runner="mock", unit=9, run_dir=str(run_dir), payload={})
            cfg = Config(backend="claude")
            with mock.patch.object(
                backend_mod, "backend_cli_version", return_value="2.1.167"
            ) as version:
                backend_mod._record_run_started(
                    cfg,
                    run_dir,
                    handle,
                    spec,
                    backend_name="claude-code-deepseek",
                    resume_ref=None,
                )
            record = json.loads(
                (run_dir / "run_started.json").read_text(encoding="utf-8")
            )
            self.assertEqual(record["backend"], "claude-code-deepseek")
            self.assertEqual(record["backend_cli_version"], "2.1.167")
            version.assert_called_once_with(cfg, "claude-code-deepseek")

    def test_recorded_backend_reuses_selected_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = Config()
            backend_mod._record_backend_selection(cfg, root, 9, "claude-code-deepseek")
            self.assertEqual(
                backend_mod.recorded_backend(cfg, root, 9, "claude"),
                "claude-code-deepseek",
            )

    def test_recorded_backend_legacy_fallback_and_malformed_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = Config()
            self.assertEqual(
                backend_mod.recorded_backend(cfg, root, 9, "claude"), "claude"
            )
            path = backend_mod._backend_selection_path(cfg, root, 9)
            path.parent.mkdir(parents=True)
            path.write_text("../../../payload\n", encoding="utf-8")
            with self.assertRaisesRegex(ForemanError, "unknown backend"):
                backend_mod.recorded_backend(cfg, root, 9, "claude")

    def test_adapter_path_rejects_traversal_even_when_target_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            trusted = root / "backends"
            trusted.mkdir()
            (trusted / "claude.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            (root / "payload.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            with mock.patch.object(backend_mod, "BACKENDS_DIR", trusted):
                with self.assertRaisesRegex(ForemanError, "unknown backend"):
                    backend_mod.adapter_path("../payload")

    def test_backend_selection_precedes_recoverable_handle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = Config()
            spec = UnitSpec(
                unit=9,
                workdir=root,
                run_dir=root,
                env={},
                cmd=("adapter", "run"),
                timeout_s=60,
            )
            handle = Handle(runner="mock", unit=9, run_dir=str(root), payload={})
            order: list[str] = []
            with (
                mock.patch.object(
                    backend_mod,
                    "_record_backend_selection",
                    side_effect=lambda *_args: order.append("backend"),
                ),
                mock.patch.object(
                    backend_mod.runner_mod,
                    "save_handle",
                    side_effect=lambda *_args: order.append("handle"),
                ),
                mock.patch.object(
                    backend_mod,
                    "_record_run_started",
                    side_effect=lambda *_args, **_kwargs: order.append("telemetry"),
                ),
            ):
                backend_mod._publish_started_run(
                    cfg,
                    root,
                    root,
                    handle,
                    spec,
                    backend_name="claude-code-deepseek",
                    resume_ref=None,
                )
            self.assertEqual(order, ["backend", "handle", "telemetry"])

    def test_version_pin_checks_effective_per_unit_adapter(self):
        cfg = Config(backend="mock", backend_version="2.1.167")
        with mock.patch.object(
            backend_mod, "backend_cli_version", return_value="2.1.167"
        ) as version:
            backend_mod.assert_backend_version(cfg, "claude-code-deepseek")
        version.assert_called_once_with(cfg, "claude-code-deepseek")


if __name__ == "__main__":
    unittest.main()
