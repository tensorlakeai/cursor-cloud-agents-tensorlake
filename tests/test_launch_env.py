"""The launcher must forward every pool setting the in-sandbox orchestrator reads,
and restart the orchestrator when those settings change in .env."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from cursor_tensorlake.launch_orchestrator_sandbox import (
    ORCHESTRATOR_PROCESS_NAME,
    ensure_orchestrator,
    forwarded_env,
    settings_differ,
)

from ._fakes import FakeProcess, FakeResult, FakeSandbox, make_config

# A heartbeat from an orchestrator running the defaults of ``make_config``.
# Spelled out rather than derived, so a new setting that the orchestrator does
# not echo shows up here as a failure.
MATCHING = {
    "pool_mode": "repo",
    "pool_repo_url": None,
    "computer_use": False,
    "display": ":1",
    "share_desktop": None,
}


class ForwardedEnvTests(unittest.TestCase):
    def test_pool_settings_reach_the_orchestrator(self) -> None:
        saved = dict(os.environ)
        try:
            os.environ.update({
                "CURSOR_API_KEY": "sa_x",
                "CURSOR_POOL": "tensorlake",
                "CURSOR_POOL_MODE": "any-repo",
                "CURSOR_POOL_REPO_URL": "",
                "TENSORLAKE_API_KEY": "tl_x",
                "IMAGE_NAME": "cursor-tl-worker-abc12345",
            })
            env = forwarded_env()
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertEqual(env["CURSOR_POOL_MODE"], "any-repo")
        self.assertEqual(env["CURSOR_POOL"], "tensorlake")
        self.assertNotIn("CURSOR_POOL_REPO_URL", env)  # blank values are not forwarded
        self.assertEqual(env["PATH"], "/usr/local/bin:/usr/bin:/bin")

    def test_computer_use_settings_reach_the_orchestrator(self) -> None:
        # The spawn hook runs inside the orchestrator sandbox and reads these
        # from its own environment. Unforwarded, a worker starts with no
        # --computer-use however the local .env is set.
        saved = dict(os.environ)
        try:
            os.environ.update({
                "CURSOR_API_KEY": "sa_x",
                "TENSORLAKE_API_KEY": "tl_x",
                "IMAGE_NAME": "cursor-tl-worker-abc12345",
                "WORKER_COMPUTER_USE": "true",
                "WORKER_DISPLAY": ":1",
                "WORKER_SHARE_DESKTOP": "view",
            })
            env = forwarded_env()
        finally:
            os.environ.clear()
            os.environ.update(saved)
        self.assertEqual(env["WORKER_COMPUTER_USE"], "true")
        self.assertEqual(env["WORKER_DISPLAY"], ":1")
        self.assertEqual(env["WORKER_SHARE_DESKTOP"], "view")


class SettingsDifferTests(unittest.TestCase):
    def test_matching_heartbeat_is_not_a_difference(self) -> None:
        config, _ = make_config()
        self.assertFalse(settings_differ(dict(MATCHING), config))

    def test_no_heartbeat_yet_is_not_a_difference(self) -> None:
        config, _ = make_config()
        self.assertFalse(settings_differ(None, config))

    def test_mode_change_is_a_difference(self) -> None:
        config, _ = make_config()  # .env says repo
        self.assertTrue(settings_differ({**MATCHING, "pool_mode": "any-repo"}, config))

    def test_repo_pin_change_is_a_difference(self) -> None:
        config, _ = make_config(CURSOR_POOL_REPO_URL="https://github.com/acme/widgets")
        self.assertTrue(settings_differ(dict(MATCHING), config))

    def test_old_heartbeat_without_settings_is_a_difference(self) -> None:
        config, _ = make_config()
        self.assertTrue(settings_differ({"state": "watching"}, config))

    def test_computer_use_change_is_a_difference(self) -> None:
        config, _ = make_config(WORKER_COMPUTER_USE="true")
        self.assertTrue(settings_differ(dict(MATCHING), config))


class EnsureOrchestratorTests(unittest.TestCase):
    def _running(self, heartbeat: dict | None) -> FakeSandbox:
        sandbox = FakeSandbox(name="cursor-tl-orch-x", bind_outcome="attached")
        sandbox.processes[ORCHESTRATOR_PROCESS_NAME] = FakeProcess("bash", [], name=ORCHESTRATOR_PROCESS_NAME)
        if heartbeat is not None:
            sandbox.script_result("status.json", FakeResult(0, json.dumps(heartbeat)))
        return sandbox

    def _ensure(self, sandbox: FakeSandbox, config, **kwargs) -> list[str]:
        lines: list[str] = []
        with mock.patch("tensorlake.sandbox.Sandbox.get_or_create", return_value=sandbox), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: lines.append(" ".join(str(x) for x in a))):
            ensure_orchestrator(config, **kwargs)
        return lines

    def test_running_process_with_matching_settings_is_kept(self) -> None:
        config, _ = make_config()
        sandbox = self._running(dict(MATCHING))
        lines = self._ensure(sandbox, config)
        self.assertEqual(sandbox.started, [])
        self.assertTrue(any("already running" in line for line in lines), lines)

    def test_changed_settings_restart_the_process(self) -> None:
        config, _ = make_config()  # .env now says repo
        sandbox = self._running({**MATCHING, "pool_mode": "any-repo"})
        lines = self._ensure(sandbox, config)
        self.assertEqual(len(sandbox.started), 1)
        # The new process starts from the current environment, where the
        # default mode is not set, so the orchestrator falls back to `repo`.
        self.assertEqual(sandbox.started[0]["env"].get("CURSOR_POOL_MODE", "repo"), "repo")
        self.assertTrue(any("settings changed" in line for line in lines), lines)

    def test_restart_flag_replaces_a_matching_process(self) -> None:
        config, _ = make_config()
        sandbox = self._running(dict(MATCHING))
        self._ensure(sandbox, config, restart=True)
        self.assertEqual(len(sandbox.started), 1)

    def test_recreate_terminates_the_old_sandbox_first(self) -> None:
        config, _ = make_config()
        old = self._running(dict(MATCHING))
        new = FakeSandbox(name="cursor-tl-orch-x", bind_outcome="created")
        lines: list[str] = []
        with mock.patch("cursor_tensorlake.launch_orchestrator_sandbox.connect_sandbox", return_value=old), \
                mock.patch("tensorlake.sandbox.Sandbox.get_or_create", return_value=new), \
                mock.patch("builtins.print", side_effect=lambda *a, **k: lines.append(" ".join(str(x) for x in a))):
            ensure_orchestrator(config, recreate=True)
        self.assertTrue(old.terminated)
        self.assertEqual(len(new.started), 1)
        self.assertTrue(any("Terminated the old sandbox" in line for line in lines), lines)

    def test_terminate_waits_until_the_name_is_free(self) -> None:
        # Termination is asynchronous: the old sandbox still answers as
        # "running" for a while. The launcher must not attach to it.
        from cursor_tensorlake.launch_orchestrator_sandbox import _terminate_and_wait

        config, _ = make_config()
        old = FakeSandbox(name="cursor-tl-orch-x")
        polls = iter(["running", "terminating", "terminated"])

        def connect(cfg, name):
            old.status = next(polls, "terminated")
            return old

        slept: list[float] = []
        ticks = iter(range(0, 100))
        with mock.patch("cursor_tensorlake.launch_orchestrator_sandbox.connect_sandbox", side_effect=connect):
            done = _terminate_and_wait(config, "cursor-tl-orch-x", sleep=slept.append, clock=lambda: float(next(ticks)))
        self.assertTrue(done)
        self.assertTrue(old.terminated)
        # First connect finds it "running", terminate, poll "terminating" (sleep), poll "terminated".
        self.assertEqual(len(slept), 1)

    def test_terminate_gives_up_after_the_timeout(self) -> None:
        from cursor_tensorlake.launch_orchestrator_sandbox import _terminate_and_wait

        config, _ = make_config()
        stuck = FakeSandbox(name="cursor-tl-orch-x")
        stuck.terminate = lambda **_: None  # never reaches a gone status
        ticks = iter(range(0, 1000))
        with mock.patch("cursor_tensorlake.launch_orchestrator_sandbox.connect_sandbox", return_value=stuck):
            with self.assertRaises(RuntimeError):
                _terminate_and_wait(config, "cursor-tl-orch-x", timeout_secs=5, sleep=lambda s: None, clock=lambda: float(next(ticks)))

    def test_no_process_starts_one(self) -> None:
        config, _ = make_config()
        sandbox = FakeSandbox(name="cursor-tl-orch-x", bind_outcome="created")
        self._ensure(sandbox, config)
        self.assertEqual(len(sandbox.started), 1)
        self.assertEqual(sandbox.started[0]["name"], ORCHESTRATOR_PROCESS_NAME)


if __name__ == "__main__":
    unittest.main()
