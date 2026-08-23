"""Config loading: defaults, TOML, tables, env overrides, fail-loud validation."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from foreman import config as config_mod
from foreman.util import ForemanError


class ConfigLoading(unittest.TestCase):
    def load_toml(self, text: str):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / config_mod.CONFIG_FILE).write_text(text, encoding="utf-8")
            return config_mod.load(root)

    def test_defaults_without_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config_mod.load(Path(tmp))
        self.assertEqual(cfg.verify, {"default": ["task", "verify"]})
        self.assertEqual(cfg.required_capabilities, [])
        self.assertEqual(cfg.trusted_actors, [])
        self.assertEqual(cfg.reviewer_login, "")
        self.assertEqual(cfg.reviewer_request, "")
        self.assertEqual(cfg.reviewer_timeout_min, 10)
        self.assertEqual(cfg.reviewer_max_attempts, 2)
        self.assertEqual(cfg.runner, "local")
        self.assertEqual(cfg.image, "")
        self.assertTrue(cfg.require_approval)
        self.assertEqual(cfg.billing, "subscription")
        self.assertEqual(cfg.max_parallel, 3)

    def test_dogfood_config_loads(self):
        # The repo's own .foreman.toml must always parse: in TOML, top-level
        # keys placed after a [section] header silently belong to that
        # section, and the unknown-key guards then reject them (caught live
        # during #13 bring-up — required_capabilities/billing/sandboxed had
        # drifted under [verify]).
        root = Path(__file__).resolve().parents[1]
        cfg = config_mod.load(root)
        self.assertEqual(cfg.expected_login, "evanharmon1-bot")
        self.assertEqual(cfg.billing, "subscription")
        self.assertEqual(cfg.required_capabilities, [])
        self.assertIn("docker", cfg.verify)
        self.assertEqual(
            cfg.trusted_actors,
            [
                "evanharmon1",
                "evanharmon1-bot",
                "admiralfraggle",
                "admiralfraggle-bot",
                "Jessedroptable",
                "chatgpt-codex-connector[bot]",
            ],
        )
        self.assertEqual(cfg.runner, "local")
        # Shape, not identity: bumping the pin (`task image:pin:set`) must
        # never require editing a test.
        self.assertTrue(
            cfg.image.startswith("ghcr.io/ponderousdev/foreman-devcontainer"),
            f"unexpected dogfood image pin: {cfg.image}",
        )
        self.assertIsNotNone(config_mod.IMAGE_PIN_RE.fullmatch(cfg.image))
        self.assertTrue(cfg.require_approval)
        self.assertEqual(cfg.max_parallel, 3)

    def test_toml_values_and_tables(self):
        cfg = self.load_toml(
            'backend = "mock"\n'
            'trusted_actors = ["evan", "coderabbitai[bot]"]\n'
            "[verify]\n"
            'default = ["task", "verify"]\n'
            'docker = ["task", "verify:docker"]\n'
            "[budgets]\ndispatch_usd = 5.0\n"
            "[timeouts]\nshepherd_min = 10\n"
        )
        self.assertEqual(cfg.backend, "mock")
        self.assertEqual(
            cfg.verify,
            {"default": ["task", "verify"], "docker": ["task", "verify:docker"]},
        )
        self.assertEqual(cfg.trusted_actors, ["evan", "coderabbitai[bot]"])
        self.assertEqual(cfg.dispatch_budget_usd, 5.0)
        self.assertEqual(cfg.shepherd_timeout_min, 10)

    def test_reviewer_table(self):
        cfg = self.load_toml(
            "[reviewer]\n"
            'login = "review-bot[bot]"\n'
            'request = "@review-bot review"\n'
            "timeout_min = 15\n"
            "max_attempts = 3\n"
        )
        self.assertEqual(cfg.reviewer_login, "review-bot[bot]")
        self.assertEqual(cfg.reviewer_request, "@review-bot review")
        self.assertEqual(cfg.reviewer_timeout_min, 15)
        self.assertEqual(cfg.reviewer_max_attempts, 3)

    def test_reviewer_login_requires_request(self):
        with self.assertRaisesRegex(ForemanError, "request is required"):
            self.load_toml('[reviewer]\nlogin = "review-bot[bot]"\n')

    def test_reviewer_request_requires_login(self):
        with self.assertRaisesRegex(ForemanError, "login is required"):
            self.load_toml('[reviewer]\nrequest = "@review-bot review"\n')

    def test_reviewer_fields_have_strict_types_and_cannot_forge_markers(self):
        for text in (
            "[reviewer]\nlogin = 0\n",
            "[reviewer]\nrequest = 0\n",
            '[reviewer]\nlogin = " review-bot[bot]"\nrequest = "@review-bot review"\n',
            "[reviewer]\n"
            'login = "review-bot[bot]"\n'
            'request = "<!-- foreman:review-request head=x attempt=9 -->"\n',
        ):
            with self.subTest(text=text):
                with self.assertRaises(ForemanError):
                    self.load_toml(text)

    def test_reviewer_bounds_must_be_positive_integers(self):
        for key, value in (("timeout_min", "0"), ("max_attempts", "false")):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ForemanError, key):
                    self.load_toml(f"[reviewer]\n{key} = {value}\n")

    def test_legacy_verify_command_maps_to_the_gate_baseline(self):
        cfg = self.load_toml('verify_command = ["task", "ci"]\n')
        self.assertEqual(cfg.verify, {"default": ["task", "ci"]})

    def test_verify_command_alongside_verify_table_fails(self):
        with self.assertRaises(ForemanError):
            self.load_toml(
                'verify_command = ["task", "ci"]\n[verify]\ndefault = ["task", "verify"]\n'
            )

    def test_unknown_verify_capability_key_fails(self):
        with self.assertRaises(ForemanError) as ctx:
            self.load_toml(
                '[verify]\ndefault = ["task", "verify"]\nport = ["task", "e2e"]\n'
            )
        self.assertIn("port", str(ctx.exception))
        self.assertIn("ports", str(ctx.exception))

    def test_unknown_required_capability_fails(self):
        with self.assertRaises(ForemanError) as ctx:
            self.load_toml('required_capabilities = ["dokcer"]\n')
        self.assertIn("dokcer", str(ctx.exception))

    def test_verify_without_default_fails(self):
        with self.assertRaises(ForemanError):
            self.load_toml('[verify]\ndocker = ["task", "verify:docker"]\n')

    def test_unknown_runner_fails(self):
        with self.assertRaises(ForemanError):
            self.load_toml('runner = "qemu"\n')

    def test_image_pin_accepts_digest_refs(self):
        digest = "a" * 64
        for ref in (
            f"ghcr.io/ponderousdev/foreman-devcontainer@sha256:{digest}",
            f"ghcr.io/ponderousdev/foreman-devcontainer:sha-{'b' * 40}@sha256:{digest}",
            f"localhost:5000/x:tag@sha256:{digest}",
            f"ghcr.io/org/my--image@sha256:{digest}",
            f"ghcr.io/org/a__b@sha256:{digest}",
        ):
            with self.subTest(ref=ref):
                cfg = self.load_toml(f'image = "{ref}"\n')
                self.assertEqual(cfg.image, ref)

    def test_image_without_a_valid_digest_fails(self):
        # Pin grammar is digest-required; the tag alone is mutable.
        for ref in (
            "ghcr.io/ponderousdev/foreman-devcontainer:latest",
            f"ghcr.io/o/r@sha256:{'a' * 63}",
            f"ghcr.io/o/r@sha256:{'A' * 64}",
            f"ghcr.io/o/r@sha512:{'a' * 64}",
            f"ghcr.io-/ns/img@sha256:{'a' * 64}",
            f"ghcr..io/ns/img@sha256:{'a' * 64}",
        ):
            with self.subTest(ref=ref):
                with self.assertRaises(ForemanError) as ctx:
                    self.load_toml(f'image = "{ref}"\n')
                self.assertIn("pin by digest", str(ctx.exception))

    def test_image_must_be_a_string(self):
        with self.assertRaisesRegex(ForemanError, "image must be a string"):
            self.load_toml("image = 123\n")

    def test_image_digest_extracts_the_token(self):
        digest = "a" * 64
        self.assertEqual(
            config_mod.image_digest(f"ghcr.io/o/r:tag@sha256:{digest}"),
            f"sha256:{digest}",
        )
        self.assertEqual(config_mod.image_digest("ghcr.io/o/r:latest"), "")
        self.assertEqual(config_mod.image_digest(""), "")

    def test_shell_pin_regex_agrees_with_python(self):
        # scripts/agent-image-pin.sh carries a POSIX ERE twin of IMAGE_PIN_RE
        # so `task image:pin:set` rejects exactly what config.py would reject.
        # Two hand-maintained grammars WILL drift; this pins them together.
        root = Path(__file__).resolve().parents[1]
        script = (root / "scripts" / "agent-image-pin.sh").read_text(encoding="utf-8")
        match = re.search(r"^pin_re='(?P<re>.*)'$", script, re.M)
        self.assertIsNotNone(match, "could not find pin_re= in agent-image-pin.sh")
        shell_re = match.group("re")

        good, bad = "a" * 64, "a" * 63
        vectors = (
            f"ghcr.io/ponderousdev/foreman-devcontainer@sha256:{good}",
            f"ghcr.io/ponderousdev/foreman-devcontainer:sha-{'b' * 40}@sha256:{good}",
            f"localhost:5000/x:tag@sha256:{good}",
            f"ghcr.io/org/my--image@sha256:{good}",
            f"ghcr.io/org/a__b@sha256:{good}",
            f"ghcr.io/org/a.b/c_d@sha256:{good}",
            f"img@sha256:{good}",
            "ghcr.io/ponderousdev/foreman-devcontainer:latest",
            f"ghcr.io/o/r@sha256:{bad}",
            f"ghcr.io/o/r@sha256:{'A' * 64}",
            f"ghcr.io/o/r@sha512:{good}",
            f"ghcr.io-/ns/img@sha256:{good}",
            f"ghcr..io/ns/img@sha256:{good}",
            f"ghcr.io/O/img@sha256:{good}",
            f"ghcr.io/o/r@sha256:{good} trailing",
            f" ghcr.io/o/r@sha256:{good}",
        )
        for ref in vectors:
            with self.subTest(ref=ref):
                python_ok = config_mod.IMAGE_PIN_RE.fullmatch(ref) is not None
                shell_ok = (
                    subprocess.run(
                        ["grep", "-Eq", shell_re],
                        input=ref + "\n",
                        text=True,
                        check=False,
                        # The helper pins LC_ALL=C so its ERE bracket ranges
                        # are ASCII like IMAGE_PIN_RE; mirror that here.
                        env={**os.environ, "LC_ALL": "C"},
                    ).returncode
                    == 0
                )
                self.assertEqual(
                    python_ok,
                    shell_ok,
                    f"python={python_ok} shell={shell_ok} disagree on {ref!r}",
                )

    def test_env_override_wins(self):
        os.environ["FOREMAN_BACKEND"] = "mock"
        try:
            cfg = self.load_toml('backend = "claude"\n')
        finally:
            del os.environ["FOREMAN_BACKEND"]
        self.assertEqual(cfg.backend, "mock")

    def test_invalid_inputs_mode_fails(self):
        with self.assertRaises(ForemanError):
            self.load_toml('inputs = "psychic"\n')

    def test_empty_gate_command_fails(self):
        with self.assertRaises(ForemanError):
            self.load_toml("[verify]\ndefault = []\n")

    def test_multi_segment_branch_prefix_fails(self):
        with self.assertRaises(ForemanError):
            self.load_toml('branch_prefix = "bots/foreman"\n')

    def test_permission_mode_derivation(self):
        self.assertEqual(
            self.load_toml("sandboxed = true\n").resolved_permission_mode(),
            "bypassPermissions",
        )
        self.assertEqual(self.load_toml("").resolved_permission_mode(), "acceptEdits")


if __name__ == "__main__":
    unittest.main()
