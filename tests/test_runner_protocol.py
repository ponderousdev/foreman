"""Runner protocol: mock satisfies it; handles round-trip and stay honest."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from foreman import runner as runner_mod
from foreman.config import Config
from foreman.runner import ExitStatus, Handle, Runner, UnitSpec, WaitTimeout
from foreman.util import ForemanError
from tests.mock_runner import MockRunner


def spec_for(unit: int, root: Path) -> UnitSpec:
    return UnitSpec(
        unit=unit,
        workdir=root / "wt",
        run_dir=root / "run",
        env={"PATH": "/usr/bin"},
        cmd=("adapter", "run"),
        timeout_s=60,
    )


class MockSatisfiesProtocol(unittest.TestCase):
    def test_mock_is_a_runner(self):
        self.assertIsInstance(MockRunner(), Runner)

    def test_spawn_wait_reports_agent_exit_code(self):
        mock = MockRunner(agent=lambda spec: 7)
        with tempfile.TemporaryDirectory() as tmp:
            handle = mock.spawn(spec_for(3, Path(tmp)))
        status = mock.wait(handle, timeout_s=0)
        self.assertEqual(status.code, 7)
        self.assertFalse(status.ok)
        self.assertFalse(status.abnormal)

    def test_running_unit_raises_wait_timeout_never_a_status(self):
        mock = MockRunner()
        with tempfile.TemporaryDirectory() as tmp:
            handle = mock.spawn(spec_for(4, Path(tmp)))
        mock.running.add(4)
        with self.assertRaises(WaitTimeout):
            mock.wait(handle, timeout_s=0)
        mock.kill(handle)
        status = mock.wait(handle, timeout_s=0)
        self.assertEqual(status.code, 128 + 15)

    def test_unknown_unit_is_abnormal_not_guessed(self):
        mock = MockRunner()
        status = mock.wait(
            Handle(runner="mock", unit=99, run_dir="/nowhere"), timeout_s=0
        )
        self.assertTrue(status.abnormal)
        self.assertIsNone(status.code)

    def test_ok_requires_zero_and_not_abnormal(self):
        self.assertTrue(ExitStatus(code=0).ok)
        self.assertFalse(ExitStatus(code=0, abnormal=True).ok)
        self.assertFalse(ExitStatus(code=1).ok)
        self.assertFalse(ExitStatus(code=None, abnormal=True).ok)


class HandleStore(unittest.TestCase):
    def test_round_trip_and_delete(self):
        cfg = Config()
        handle = Handle(
            runner="mock", unit=12, run_dir="/r", payload={"pid": 42, "start": "9"}
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = runner_mod.save_handle(cfg, root, handle)
            self.assertEqual(path, runner_mod.handle_path(cfg, root, 12))
            loaded = runner_mod.load_handle(cfg, root, 12)
            assert loaded is not None
            self.assertEqual(loaded.unit, 12)
            self.assertEqual(loaded.payload, {"pid": 42, "start": "9"})
            self.assertEqual(loaded.runner, "mock")
            runner_mod.delete_handle(cfg, root, 12)
            self.assertIsNone(runner_mod.load_handle(cfg, root, 12))

    def test_missing_handle_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(runner_mod.load_handle(Config(), Path(tmp), 1))

    def test_unknown_schema_fails_loud(self):
        with self.assertRaises(ForemanError):
            Handle.from_json('{"schema": 99, "runner": "x", "unit": 1, "run_dir": ""}')


class Registry(unittest.TestCase):
    def test_sprite_and_docker_name_their_milestone(self):
        for name, note in (("sprite", "v2.1"), ("docker", "v2.2")):
            cfg = Config(runner=name)
            with self.assertRaises(ForemanError) as ctx:
                runner_mod.create(cfg)
            self.assertIn(note, str(ctx.exception))
            self.assertIn("local", str(ctx.exception))

    def test_unknown_runner_refused_at_config_load(self):
        import foreman.config as config_mod

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".foreman.toml").write_text('runner = "qemu"\n', encoding="utf-8")
            with self.assertRaises(ForemanError) as ctx:
                config_mod.load(root)
            self.assertIn("runner", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
