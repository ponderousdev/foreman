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
            session = run_dir / "session"
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
