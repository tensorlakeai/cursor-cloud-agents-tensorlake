from __future__ import annotations

import unittest
from typing import Any

from cursor_tensorlake.config import WORKER_PROCESS_NAME
from cursor_tensorlake.orchestrator import (
    ORPHAN_GRACE_SECONDS,
    WAKE_GRACE_SECONDS,
    Orchestrator,
    controller_command,
)
from cursor_tensorlake.state import StateStore, WorkerRecord

from ._fakes import FakeInfo, FakeProcess, FakeSandbox, make_config


class FakeAPI:
    def __init__(self) -> None:
        self.pending: list[dict[str, Any]] = []

    def list_pending_requests(self, pool=None, limit=100):
        return {"requests": list(self.pending)}


class ManualClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class OrchestratorFixture:
    def __init__(self, **config_overrides: Any) -> None:
        self.config, self.state_dir = make_config(**config_overrides)
        self.store = StateStore(self.state_dir)
        self.api = FakeAPI()
        self.clock = ManualClock()
        self.infos: list[FakeInfo] = []
        self.sandboxes: dict[str, FakeSandbox] = {}
        self.spawned: list[Any] = []
        self.orch = Orchestrator(
            self.config,
            self.api,  # type: ignore[arg-type]
            self.store,
            list_sandboxes=lambda cfg: list(self.infos),
            connect=lambda cfg, name: self.sandboxes.get(name),
            spawn=lambda cfg, claim: self.spawned.append(claim),
            clock=self.clock,
        )

    def add(self, name: str, status: str = "running", worker: str | None = "running") -> FakeSandbox:
        self.infos.append(FakeInfo(f"id-{name}", name, status))
        sandbox = FakeSandbox(sandbox_id=f"id-{name}", name=name, status=status)
        if worker is not None:
            sandbox.processes[WORKER_PROCESS_NAME] = FakeProcess("bash", [], name=WORKER_PROCESS_NAME, status=worker)
        self.sandboxes[name] = sandbox
        return sandbox


class JanitorTests(unittest.TestCase):
    def test_live_worker_is_left_alone(self) -> None:
        fx = OrchestratorFixture()
        sandbox = fx.add("cursor-w1", worker="running")
        counts = fx.orch.janitor_once()
        self.assertFalse(sandbox.suspended)
        self.assertEqual(counts["running"], 1)

    def test_exited_worker_hibernates_sandbox(self) -> None:
        fx = OrchestratorFixture()
        sandbox = fx.add("cursor-w1", worker="exited")
        counts = fx.orch.janitor_once()
        self.assertTrue(sandbox.suspended)
        self.assertEqual(counts["suspended_now"], 1)
        record = fx.store.read("cursor-w1")
        assert record is not None
        self.assertEqual(record.suspended_at, fx.clock.now)

    def test_orphan_gets_grace_then_suspends(self) -> None:
        fx = OrchestratorFixture()
        sandbox = fx.add("cursor-w1", worker=None)
        fx.store.write(WorkerRecord(sandbox_name="cursor-w1", worker_id="w1", pool="tensorlake", last_started_at=fx.clock.now))
        fx.orch.janitor_once()
        self.assertFalse(sandbox.suspended)
        fx.clock.now += ORPHAN_GRACE_SECONDS + 1
        fx.orch.janitor_once()
        self.assertTrue(sandbox.suspended)

    def test_retention_terminates_old_suspended_sandbox(self) -> None:
        fx = OrchestratorFixture(SESSION_RETENTION_SECS="100")
        sandbox = fx.add("cursor-w1", status="suspended")
        fx.orch.janitor_once()  # stamps suspended_at
        self.assertFalse(sandbox.terminated)
        fx.clock.now += 101
        fx.orch.janitor_once()
        self.assertTrue(sandbox.terminated)
        self.assertIsNone(fx.store.read("cursor-w1"))

    def test_gone_sandbox_drops_record(self) -> None:
        fx = OrchestratorFixture()
        fx.store.write(WorkerRecord(sandbox_name="cursor-w1", worker_id="w1", pool="tensorlake"))
        fx.add("cursor-w1", status="terminated", worker=None)
        fx.orch.janitor_once()
        self.assertIsNone(fx.store.read("cursor-w1"))

    def test_status_heartbeat_written(self) -> None:
        fx = OrchestratorFixture()
        fx.orch.janitor_once()
        self.assertTrue(fx.store.status_path.exists())
        text = fx.store.status_path.read_text()
        self.assertIn('"state": "watching"', text)
        # The launcher reads these to restart the orchestrator when .env changes.
        self.assertIn('"pool_mode": "repo"', text)
        self.assertIn('"pool_repo_url": null', text)


class WakeTests(unittest.TestCase):
    def test_claimed_offline_wakes_owned_sandbox_after_grace(self) -> None:
        fx = OrchestratorFixture()
        fx.add("cursor-worker-1", status="suspended", worker=None)
        fx.api.pending = [{"id": "bc-1", "claimedWorkerId": "worker-1", "wakeTimeoutMs": 900000, "repoUrl": "github.com/a/b"}]
        self.assertEqual(fx.orch.wake_once(), [])  # first sighting: grace
        fx.clock.now += WAKE_GRACE_SECONDS + 1
        self.assertEqual(fx.orch.wake_once(), ["worker-1"])
        claim = fx.spawned[0]
        self.assertEqual(claim.worker_id, "worker-1")
        self.assertEqual(claim.request_id, "bc-1")
        self.assertEqual(claim.primary_origin_url(), "https://github.com/a/b")
        # Not repeated immediately.
        self.assertEqual(fx.orch.wake_once(), [])

    def test_unowned_worker_ignored(self) -> None:
        fx = OrchestratorFixture()
        fx.api.pending = [{"id": "bc-1", "claimedWorkerId": "someone-else"}]
        fx.clock.now += WAKE_GRACE_SECONDS + 1
        fx.orch.wake_once()
        fx.clock.now += WAKE_GRACE_SECONDS + 1
        self.assertEqual(fx.orch.wake_once(), [])
        self.assertEqual(fx.spawned, [])

    def test_plain_pending_requests_are_the_controllers_job(self) -> None:
        fx = OrchestratorFixture()
        fx.add("cursor-worker-1", status="suspended", worker=None)
        fx.api.pending = [{"id": "bc-2"}]
        fx.clock.now += WAKE_GRACE_SECONDS + 1
        self.assertEqual(fx.orch.wake_once(), [])


class ControllerCommandTests(unittest.TestCase):
    def test_command_shape(self) -> None:
        config, _ = make_config(WARM_IDLE="2")
        argv = controller_command(config, spawn_path="/usr/local/bin/cursor-tl-spawn")
        self.assertEqual(argv[:4], ["agent", "worker", "controller", "--spawn"])
        self.assertEqual(argv[4], "/usr/local/bin/cursor-tl-spawn")
        self.assertEqual(argv[argv.index("--pool") + 1], "tensorlake")
        self.assertEqual(argv[argv.index("--warm-idle") + 1], "2")
        self.assertNotIn("--repository", argv)

    def test_repository_pin(self) -> None:
        config, _ = make_config(CURSOR_POOL_REPO_URL="https://github.com/acme/widgets.git")
        argv = controller_command(config, spawn_path="/usr/local/bin/cursor-tl-spawn")
        self.assertEqual(argv[argv.index("--repository") + 1], "https://github.com/acme/widgets")

    def test_any_repo_mode_has_no_repository_pin(self) -> None:
        config, _ = make_config(CURSOR_POOL_MODE="any-repo")
        argv = controller_command(config, spawn_path="/usr/local/bin/cursor-tl-spawn")
        self.assertNotIn("--repository", argv)
        self.assertEqual(argv[argv.index("--pool") + 1], "tensorlake")
