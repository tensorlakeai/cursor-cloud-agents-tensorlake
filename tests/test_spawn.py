from __future__ import annotations

import unittest

from cursor_tensorlake.config import LABELS_PATH, WORKER_PROCESS_NAME, WORKSPACE_DIR
from cursor_tensorlake.spawn import (
    CapacityError,
    SpawnError,
    active_worker_count,
    check_capacity,
    spawn_worker,
    any_repo_prep_script,
    workspace_prep_script,
)
from cursor_tensorlake.state import StateStore

from ._fakes import FakeInfo, FakeProcess, FakeResult, FakeSandbox, make_claim, make_config


def _no_sleep(_: float) -> None:
    pass


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 5.0
        return self.t


class SpawnTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config, self.state_dir = make_config(WORKER_LABELS_JSON='{"env":"test"}')
        self.store = StateStore(self.state_dir)
        self.released: list[str] = []

    def _spawn(self, sandbox: FakeSandbox, claim=None):
        return spawn_worker(
            self.config,
            claim or make_claim(),
            get_or_create=lambda cfg, name: sandbox,
            release_claim=self.released.append,
            store=self.store,
            sleep=_no_sleep,
            clock=Clock(),
        )

    def test_created_path_seeds_origin_and_starts_worker(self) -> None:
        sandbox = FakeSandbox(bind_outcome="created")
        result = self._spawn(sandbox)
        self.assertEqual(result.action, "started")
        self.assertEqual(result.bind_outcome, "created")
        prep = sandbox.runs[0]
        self.assertIn("remote add origin https://github.com/acme/widgets", " ".join(prep[1]))
        self.assertEqual(prep[2]["user"], "1000:1000")
        self.assertEqual(sandbox.written[LABELS_PATH], b'{"env":"test"}')
        started = sandbox.started[0]
        self.assertEqual(started["name"], WORKER_PROCESS_NAME)
        self.assertEqual(started["working_dir"], WORKSPACE_DIR)
        script = started["args"][1]
        self.assertNotIn("--worker-id", script)
        self.assertEqual(started["env"]["CURSOR_AGENT_WORKER_ID"], "worker-abc123")
        self.assertIn("--mint-github-token", script)
        self.assertIn("start --verbose", script)
        self.assertNotIn("CURSOR_API_URL", started["env"])
        self.assertEqual(started["env"]["CURSOR_API_KEY"], "sa_dummy_cursor_key")
        record = self.store.read("cursor-worker-abc123")
        assert record is not None
        self.assertEqual(record.worker_id, "worker-abc123")
        self.assertIsNotNone(record.last_started_at)
        self.assertIsNone(record.suspended_at)
        self.assertEqual(self.released, [])

    def test_resumed_path_restarts_worker_with_same_id(self) -> None:
        sandbox = FakeSandbox(bind_outcome="resumed")
        # A finished worker still holds the managed-process name.
        sandbox.processes[WORKER_PROCESS_NAME] = FakeProcess("bash", [], name=WORKER_PROCESS_NAME, status="exited")
        result = self._spawn(sandbox)
        self.assertEqual(result.action, "started")
        self.assertEqual(result.bind_outcome, "resumed")
        self.assertEqual(sandbox.processes[WORKER_PROCESS_NAME].status, "running")

    def test_wake_failure_does_not_release_a_claim(self) -> None:
        sandbox = FakeSandbox(bind_outcome="resumed")
        sandbox.exit_after_start = "exited"
        claim = make_claim(CURSOR_WAKE="1", CURSOR_WAKE_TIMEOUT_MS="600000")
        with self.assertRaises(SpawnError):
            self._spawn(sandbox, claim)
        self.assertEqual(self.released, [])
        self.assertTrue(sandbox.suspended)

    def test_running_worker_is_a_no_op(self) -> None:
        sandbox = FakeSandbox(bind_outcome="attached")
        sandbox.processes[WORKER_PROCESS_NAME] = FakeProcess("bash", [], name=WORKER_PROCESS_NAME)
        result = self._spawn(sandbox)
        self.assertEqual(result.action, "already-running")
        self.assertEqual(sandbox.started, [])
        self.assertEqual(sandbox.runs, [])

    def test_failure_on_created_releases_claim_and_terminates(self) -> None:
        sandbox = FakeSandbox(bind_outcome="created")
        sandbox.script_result("git", FakeResult(128, stderr="fatal: boom"))
        with self.assertRaises(SpawnError):
            self._spawn(sandbox)
        self.assertEqual(self.released, ["bc-00000000-0000-0000-0000-000000000002"])
        self.assertTrue(sandbox.terminated)
        self.assertFalse(sandbox.suspended)

    def test_failure_on_resumed_suspends_instead(self) -> None:
        sandbox = FakeSandbox(bind_outcome="resumed")
        sandbox.exit_after_start = "exited"
        with self.assertRaises(SpawnError) as ctx:
            self._spawn(sandbox)
        self.assertIn("ended during launch", str(ctx.exception))
        self.assertTrue(sandbox.suspended)
        self.assertFalse(sandbox.terminated)

    def test_warm_spawn_without_request_has_no_claim_to_release(self) -> None:
        sandbox = FakeSandbox(bind_outcome="created")
        sandbox.exit_after_start = "exited"
        claim = make_claim(CURSOR_REQUEST_ID="", CURSOR_REPO_URL="")
        with self.assertRaises(SpawnError):
            self._spawn(sandbox, claim)
        self.assertEqual(self.released, [])

    def test_multi_repo_claim_seeds_every_root_and_exposes_them(self) -> None:
        sandbox = FakeSandbox(bind_outcome="created")
        claim = make_claim(CURSOR_REPO_URLS='["github.com/acme/app", "github.com/acme/infra"]')
        self._spawn(sandbox, claim)
        prep = " ".join(sandbox.runs[0][1])
        self.assertIn("remote add origin https://github.com/acme/app", prep)
        self.assertIn("git -C /home/tl-user/repos/infra init", prep)
        self.assertIn("remote add origin https://github.com/acme/infra", prep)
        script = sandbox.started[0]["args"][1]
        self.assertIn("--worker-dir /home/tl-user/workspace --worker-dir /home/tl-user/repos/infra", script)
        self.assertIn("--mint-github-token", script)

    def test_spawn_manifest_keeps_filtered_claim_positions(self) -> None:
        sandbox = FakeSandbox(bind_outcome="created")
        claim = make_claim(
            CURSOR_REPO_URLS='["github.com/acme/app", "git@github.com:acme/bad.git", "github.com/acme/infra"]'
        )
        with self.assertLogs("cursor_tensorlake.config", "WARNING"):
            self._spawn(sandbox, claim)
        prep = " ".join(sandbox.runs[0][1])
        self.assertIn(
            "printf '%s\\t%s\\n' 2 /home/tl-user/repos/infra",
            prep,
        )

    def test_prep_script_without_repo_makes_empty_commit(self) -> None:
        script = workspace_prep_script(None)
        self.assertIn("commit -q --allow-empty", script)
        self.assertNotIn("remote", script)
        script = workspace_prep_script("https://github.com/a/b")
        self.assertIn("remote set-url origin", script)
        self.assertIn("remote add origin", script)
        script = workspace_prep_script(
            "https://github.com/a/b", [("/home/tl-user/repos/c", "https://github.com/a/c")]
        )
        self.assertEqual(script.count("remote add origin"), 2)
        self.assertIn("mkdir -p /home/tl-user/repos/c", script)
        self.assertNotIn("commit -q --allow-empty", script)

    def test_prep_script_records_extra_roots_in_request_order(self) -> None:
        script = workspace_prep_script(
            "https://github.com/a/b",
            [
                ("/home/tl-user/repos/zeta", "https://github.com/a/zeta"),
                ("/home/tl-user/repos/alpha", "https://github.com/a/alpha"),
            ],
        )
        self.assertIn(
            "printf '%s\\t%s\\n' 1 /home/tl-user/repos/zeta 2 /home/tl-user/repos/alpha > /home/tl-user/repos/.roots",
            script,
        )
        self.assertIn("mkdir -p /home/tl-user/repos &&", script)
        single = workspace_prep_script("https://github.com/a/b")
        self.assertIn("mkdir -p /home/tl-user/repos &&", single)
        self.assertIn(": > /home/tl-user/repos/.roots", single)

    def test_prep_script_keeps_root_list_when_claim_has_no_urls(self) -> None:
        # A wake, or an any-repo request, says nothing about the extra roots;
        # truncating the list would hide them from the checkout hook.
        self.assertNotIn("/home/tl-user/repos/.roots", workspace_prep_script(None))

    def test_prep_script_records_explicit_request_indexes(self) -> None:
        script = workspace_prep_script(
            "https://github.com/a/b",
            [
                ("/home/tl-user/repos/infra", "https://github.com/a/infra"),
                ("/home/tl-user/repos/docs", "https://github.com/a/docs"),
            ],
            extra_request_indexes=[2, 4],
        )
        self.assertIn(
            "printf '%s\\t%s\\n' 2 /home/tl-user/repos/infra 4 /home/tl-user/repos/docs",
            script,
        )


class CapacityTests(unittest.TestCase):
    def _spawn(self, config, sandbox: FakeSandbox, infos, released):
        return spawn_worker(
            config,
            make_claim(),
            get_or_create=lambda cfg, name: sandbox,
            release_claim=released.append,
            store=None,
            list_sandboxes=lambda cfg: infos,
            sleep=_no_sleep,
            clock=Clock(),
        )

    def test_active_count_ignores_suspended(self) -> None:
        infos = [
            FakeInfo("a", "cursor-a", "running"),
            FakeInfo("b", "cursor-b", "suspended"),
            FakeInfo("c", "cursor-c", "pending"),
            FakeInfo("d", "cursor-d", "suspending"),
        ]
        self.assertEqual(active_worker_count(infos), 2)

    def test_cap_reached_releases_claim_and_creates_nothing(self) -> None:
        config, _ = make_config(MAX_WORKERS="2")
        infos = [FakeInfo("a", "cursor-a"), FakeInfo("b", "cursor-b")]
        released: list[str] = []
        created: list[str] = []

        def get_or_create(cfg, name):
            created.append(name)
            return FakeSandbox(name=name)

        with self.assertRaises(CapacityError) as ctx:
            spawn_worker(
                config,
                make_claim(),
                get_or_create=get_or_create,
                release_claim=released.append,
                store=None,
                list_sandboxes=lambda cfg: infos,
                sleep=_no_sleep,
                clock=Clock(),
            )
        self.assertIn("limit 2", str(ctx.exception))
        self.assertEqual(released, ["bc-00000000-0000-0000-0000-000000000002"])
        self.assertEqual(created, [])

    def test_existing_sandbox_bypasses_cap(self) -> None:
        config, _ = make_config(MAX_WORKERS="1")
        sandbox = FakeSandbox(name="cursor-worker-abc123", bind_outcome="resumed")
        infos = [FakeInfo("x", "cursor-other"), FakeInfo("y", "cursor-worker-abc123", "suspended")]
        released: list[str] = []
        result = self._spawn(config, sandbox, infos, released)
        self.assertEqual(result.action, "started")
        self.assertEqual(released, [])

    def test_suspended_sandboxes_leave_room(self) -> None:
        config, _ = make_config(MAX_WORKERS="1")
        infos = [FakeInfo("x", "cursor-other", "suspended")]
        result = self._spawn(config, FakeSandbox(bind_outcome="created"), infos, [])
        self.assertEqual(result.action, "started")

    def test_zero_means_no_cap(self) -> None:
        config, _ = make_config()
        self.assertEqual(config.max_workers, 0)
        infos = [FakeInfo(str(i), f"cursor-{i}") for i in range(50)]
        check_capacity(config, "cursor-new", infos)  # no raise
        result = self._spawn(config, FakeSandbox(bind_outcome="created"), infos, [])
        self.assertEqual(result.action, "started")


class AnyRepoSpawnTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config, self.state_dir = make_config(CURSOR_POOL_MODE="any-repo")
        self.store = StateStore(self.state_dir)

    def _spawn(self, sandbox: FakeSandbox, claim=None):
        return spawn_worker(
            self.config,
            claim or make_claim(),
            get_or_create=lambda cfg, name: sandbox,
            release_claim=None,
            store=self.store,
            sleep=_no_sleep,
            clock=Clock(),
        )

    def test_any_repo_worker_has_no_origin_and_clones(self) -> None:
        sandbox = FakeSandbox(bind_outcome="created")
        claim = make_claim(CURSOR_REPO_URLS='["github.com/acme/app", "github.com/acme/infra"]')
        result = self._spawn(sandbox, claim)
        self.assertEqual(result.action, "started")
        prep = " ".join(sandbox.runs[0][1])
        self.assertNotIn("git", prep)
        self.assertNotIn("origin", prep)
        self.assertIn(f"mkdir -p {WORKSPACE_DIR}", prep)
        script = sandbox.started[0]["args"][1]
        self.assertIn("--clone-git-repos", script)
        self.assertNotIn("--on-session-start", script)
        self.assertNotIn("--mint-github-token", script)
        self.assertEqual(script.count("--worker-dir"), 1)

    def test_any_repo_prep_script_only_makes_directories(self) -> None:
        script = any_repo_prep_script()
        self.assertEqual(script, f"set -e && mkdir -p {WORKSPACE_DIR} && mkdir -p /home/tl-user/repos")
