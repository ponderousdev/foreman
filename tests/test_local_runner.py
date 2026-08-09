"""LocalRunner mechanics with real subprocesses — no Docker daemon, no
network (docs/architecture/tests.md, Tier 1). The docker capability probe is
always injected here; the live probe belongs to the environment tier."""

from __future__ import annotations

import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from foreman.config import Config
from foreman.runner import Handle, UnitSpec, WaitTimeout
from foreman.runner.local import STATUS_FILE, LocalRunner, _proc_starttime


def runner(max_parallel: int = 3, docker: bool = False) -> LocalRunner:
    return LocalRunner(Config(max_parallel=max_parallel), docker_probe=lambda: docker)


class SpecCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "wt").mkdir()

    def spec(self, *cmd: str, timeout_s: int = 30) -> UnitSpec:
        return UnitSpec(
            unit=7,
            workdir=self.root / "wt",
            run_dir=self.root / "run",
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            cmd=cmd,
            timeout_s=timeout_s,
        )


class StatusRecording(SpecCase):
    def test_exit_code_is_recorded_and_reported(self):
        lr = runner()
        handle = lr.spawn(self.spec("sh", "-c", "exit 7"))
        status = lr.wait(handle, timeout_s=10)
        self.assertEqual(status.code, 7)
        self.assertFalse(status.abnormal)
        recorded = (self.root / "run" / STATUS_FILE).read_text("utf-8")
        self.assertEqual(recorded, "7")

    def test_zero_exit_is_ok(self):
        lr = runner()
        handle = lr.spawn(self.spec("true"))
        self.assertTrue(lr.wait(handle, timeout_s=10).ok)

    def test_stale_status_never_survives_a_respawn(self):
        lr = runner()
        (self.root / "run").mkdir()
        (self.root / "run" / STATUS_FILE).write_text("0", "utf-8")
        handle = lr.spawn(self.spec("sh", "-c", "exit 3"))
        self.assertEqual(lr.wait(handle, timeout_s=10).code, 3)

    def test_recorded_status_survives_a_foreman_restart(self):
        first = runner()
        handle = first.spawn(self.spec("sh", "-c", "exit 5"))
        first.wait(handle, timeout_s=10)
        # A restarted Foreman: new runner instance, handle deserialized —
        # cannot os.wait() a process it never spawned; must read the record.
        reborn = runner()
        revived = Handle.from_json(handle.to_json())
        status = reborn.wait(revived, timeout_s=0)
        self.assertEqual(status.code, 5)
        self.assertFalse(status.abnormal)

    def test_dead_with_no_status_is_abnormal_never_guessed(self):
        lr = runner()
        handle = lr.spawn(self.spec("sleep", "30"))
        pid = int(handle.payload["pid"])
        os.killpg(pid, signal.SIGKILL)  # kills the wrapper too: nothing records
        status = lr.wait(handle, timeout_s=10)
        self.assertTrue(status.abnormal)
        self.assertIsNone(status.code)
        self.assertIn("no recorded exit status", status.detail)


class TimeoutAndKill(SpecCase):
    def test_wait_zero_is_a_liveness_poll(self):
        lr = runner()
        handle = lr.spawn(self.spec("sleep", "30"))
        with self.assertRaises(WaitTimeout):
            lr.wait(handle, timeout_s=0)
        lr.kill(handle)

    def test_kill_terminates_the_group_and_records_signal_death(self):
        # No readiness poll needed (#54): spawn() returns only after the
        # wrapper has STARTED the command — an immediate kill() always
        # finds a child to signal and always yields a recorded 128+N.
        lr = runner()
        handle = lr.spawn(self.spec("sleep", "30"))
        with self.assertRaises(WaitTimeout):
            lr.wait(handle, timeout_s=0)
        lr.kill(handle)
        status = lr.wait(handle, timeout_s=10)
        self.assertEqual(status.code, 128 + signal.SIGTERM)
        self.assertFalse(status.abnormal)  # the wrapper recorded it

    def test_kill_never_signals_a_recycled_pid(self):
        lr = runner()
        impostor = Handle(
            runner="local",
            unit=9,
            run_dir=str(self.root / "run"),
            payload={
                "pid": os.getpid(),  # a live process that is NOT the unit
                "starttime": "not-the-real-starttime",
                "status_file": str(self.root / "run" / STATUS_FILE),
            },
        )
        lr.kill(impostor)  # must be a no-op: starttime mismatch == dead
        status = lr.wait(impostor, timeout_s=0)
        self.assertTrue(status.abnormal)

    @unittest.skipUnless(
        sys.platform.startswith("linux"),
        "PID-recycling liveness reads Linux /proc/<pid>/stat by design — the "
        "local runner ships on the Linux bot devcontainer (#154)",
    )
    def test_liveness_distinguishes_recycled_pids(self):
        lr = runner()
        real = _proc_starttime(os.getpid())
        assert real is not None
        genuine = Handle(
            runner="local",
            unit=9,
            run_dir=str(self.root),
            payload={"pid": os.getpid(), "starttime": real},
        )
        self.assertTrue(lr._alive(genuine))
        impostor = Handle(
            runner="local",
            unit=9,
            run_dir=str(self.root),
            payload={"pid": os.getpid(), "starttime": "1"},
        )
        self.assertFalse(lr._alive(impostor))


class EnvironmentTotality(SpecCase):
    def test_spec_env_is_total_not_a_merge(self):
        os.environ["FOREMAN_TEST_CANARY"] = "leaked"
        self.addCleanup(os.environ.pop, "FOREMAN_TEST_CANARY", None)
        lr = runner()
        handle = lr.spawn(
            UnitSpec(
                unit=7,
                workdir=self.root / "wt",
                run_dir=self.root / "run",
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "MARKER": "present",
                },
                cmd=("sh", "-c", "env > handed-env; exit 0"),
                timeout_s=30,
            )
        )
        self.assertTrue(lr.wait(handle, timeout_s=10).ok)
        dump = (self.root / "wt" / "handed-env").read_text("utf-8")
        self.assertIn("MARKER=present", dump)
        self.assertNotIn("FOREMAN_TEST_CANARY", dump)


class Capabilities(SpecCase):
    def test_docker_is_probed_not_assumed(self):
        self.assertEqual(runner(docker=True).capabilities(), {"docker"})
        self.assertEqual(runner(docker=False).capabilities(), set())

    def test_ports_withheld_even_at_concurrency_one(self):
        # D9: physically true at max_parallel == 1, deliberately not
        # advertised in v2.0. Implement the advertisement, not the table.
        for parallel in (1, 3):
            caps = runner(max_parallel=parallel, docker=True).capabilities()
            self.assertNotIn("ports", caps)
            self.assertNotIn("untrusted-input", caps)


class Reference(SpecCase):
    def test_reference_names_the_spawned_pid(self):
        # #126: display-only live-process reference for the narration line.
        lr = runner()
        handle = lr.spawn(self.spec("true"))
        self.assertEqual(lr.reference(handle), f"pid={handle.payload['pid']}")
        lr.wait(handle, timeout_s=10)

    def test_reference_degrades_instead_of_raising_on_a_broken_handle(self):
        broken = Handle(runner="local", unit=7, run_dir=".", payload={})
        self.assertEqual(runner().reference(broken), "pid=?")


class ExecAndLogs(SpecCase):
    def test_exec_runs_in_the_workdir(self):
        lr = runner()
        handle = lr.spawn(self.spec("true"))
        lr.wait(handle, timeout_s=10)
        result = lr.exec(handle, ["pwd"])
        self.assertEqual(result.returncode, 0)
        # Realpath-normalize both sides: pwd prints the physical path, and on
        # macOS the tmpdir is a /var → /private/var symlink (#154).
        self.assertEqual(
            Path(result.stdout.strip()).resolve(), (self.root / "wt").resolve()
        )

    def test_logs_yield_adapter_stdout(self):
        lr = runner()
        handle = lr.spawn(self.spec("sh", "-c", "echo from-the-unit"))
        lr.wait(handle, timeout_s=10)
        self.assertIn("from-the-unit", list(lr.logs(handle)))

    def test_preserve_writes_the_marker(self):
        lr = runner()
        handle = lr.spawn(self.spec("true"))
        lr.wait(handle, timeout_s=10)
        lr.preserve(handle)
        self.assertTrue((self.root / "run" / "preserved").exists())


class WaitDeadline(SpecCase):
    def test_wait_raises_within_a_bounded_window(self):
        lr = runner()
        handle = lr.spawn(self.spec("sleep", "30"))
        started = time.monotonic()
        with self.assertRaises(WaitTimeout):
            lr.wait(handle, timeout_s=1)
        self.assertLess(time.monotonic() - started, 5)
        lr.kill(handle)


if __name__ == "__main__":
    unittest.main()
