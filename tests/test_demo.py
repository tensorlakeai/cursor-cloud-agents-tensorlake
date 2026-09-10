"""The demo recorder attaches to the right worker and only ever watches."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from cursor_tensorlake.demo import (
    ARTIFACT_DIR,
    RECORDER_PATH,
    RECORDER_PROCESS_NAME,
    RECORDER_SOURCE,
    build_parser,
    install_recorder,
    list_artifacts,
    newest_worker,
    recorder_running,
    start_recorder,
    stop_recorder,
    wait_for_worker,
)

from ._fakes import FakeProcess, FakeResult, FakeSandbox, make_config


class FakeInfo:
    def __init__(self, name: str, status: str = "running", created_at: int = 0) -> None:
        self.name = name
        self.status = status
        self.created_at = created_at


class NewestWorkerTests(unittest.TestCase):
    def test_no_workers(self) -> None:
        self.assertIsNone(newest_worker([]))

    def test_newest_of_several(self) -> None:
        infos = [FakeInfo("cursor-a", created_at=10), FakeInfo("cursor-b", created_at=30)]
        picked = newest_worker(infos)
        assert picked is not None
        self.assertEqual(picked.name, "cursor-b")

    def test_a_running_worker_beats_a_newer_suspended_one(self) -> None:
        # A suspended sandbox has no desktop to film, however recent it is.
        infos = [
            FakeInfo("cursor-old", status="running", created_at=10),
            FakeInfo("cursor-new", status="suspended", created_at=99),
        ]
        picked = newest_worker(infos)
        assert picked is not None
        self.assertEqual(picked.name, "cursor-old")

    def test_all_suspended_still_returns_one(self) -> None:
        infos = [FakeInfo("cursor-a", status="suspended", created_at=1)]
        picked = newest_worker(infos)
        assert picked is not None
        self.assertEqual(picked.name, "cursor-a")

    def test_running_only_refuses_a_suspended_leftover(self) -> None:
        # `watch` waits for the worker of the request just sent. A suspended
        # sandbox from an earlier run has no desktop, so it is not a match.
        infos = [FakeInfo("cursor-old", status="suspended", created_at=1)]
        self.assertIsNone(newest_worker(infos, running_only=True))


class WaitForWorkerTests(unittest.TestCase):
    def test_returns_as_soon_as_one_appears(self) -> None:
        config, _ = make_config()
        batches = iter([
            [],
            [FakeInfo("cursor-leftover", status="suspended", created_at=1)],
            [FakeInfo("cursor-x")],
        ])
        ticks = iter(range(0, 100))
        with mock.patch("cursor_tensorlake.demo.list_session_sandboxes", side_effect=lambda _: next(batches)), \
                mock.patch("builtins.print"):
            info = wait_for_worker(config, sleep=lambda _: None, clock=lambda: float(next(ticks)))
        self.assertEqual(info.name, "cursor-x")

    def test_gives_up_with_a_pointer_to_the_logs(self) -> None:
        from cursor_tensorlake.config import ConfigError

        config, _ = make_config()
        ticks = iter(range(0, 1000))
        with mock.patch("cursor_tensorlake.demo.list_session_sandboxes", return_value=[]), \
                mock.patch("builtins.print"):
            with self.assertRaises(ConfigError) as caught:
                wait_for_worker(
                    config, timeout_secs=5, sleep=lambda _: None, clock=lambda: float(next(ticks))
                )
        self.assertIn("cursor-tl-pool pending", str(caught.exception))


class RecorderTests(unittest.TestCase):
    def test_install_writes_the_shipped_script(self) -> None:
        sandbox = FakeSandbox(name="cursor-x")
        install_recorder(sandbox)
        self.assertEqual(sandbox.written[RECORDER_PATH], RECORDER_SOURCE.read_bytes())

    def test_recorder_runs_as_the_desktop_user_on_the_pinned_display(self) -> None:
        sandbox = FakeSandbox(name="cursor-x")
        start_recorder(sandbox, display=":1", fps=12)
        self.assertEqual(len(sandbox.started), 1)
        started = sandbox.started[0]
        self.assertEqual(started["name"], RECORDER_PROCESS_NAME)
        self.assertEqual(started["user"], "1000:1000")
        self.assertEqual(started["env"]["RECORD_FPS"], "12")
        script = started["args"][-1]
        self.assertIn(ARTIFACT_DIR, script)
        self.assertIn(":1", script)

    def test_no_pinned_display_lets_the_recorder_find_it(self) -> None:
        sandbox = FakeSandbox(name="cursor-x")
        start_recorder(sandbox, display=None, fps=10)
        script = sandbox.started[0]["args"][-1]
        self.assertTrue(script.rstrip().endswith("2>&1"), script)
        self.assertNotIn(":1", script)

    def test_the_recorder_publishes_its_pid_for_a_clean_stop(self) -> None:
        script = RECORDER_SOURCE.read_text()
        self.assertIn('echo $$ > "$OUT/recorder.pid"', script)
        self.assertIn("kill -INT", script)

    def test_the_recorder_only_ever_watches(self) -> None:
        # The demo's claim is that Cursor drives the desktop. Nothing here may
        # move a pointer, press a key, or open a window.
        script = RECORDER_SOURCE.read_text()
        for driving in ("xdotool key", "xdotool click", "xdotool type",
                        "xdotool mousemove", "google-chrome", "firefox "):
            self.assertNotIn(driving, script, f"the recorder must not run `{driving}`")
        self.assertIn("x11grab", script)

    def test_stop_is_quiet_when_nothing_runs(self) -> None:
        sandbox = FakeSandbox(name="cursor-x")
        self.assertFalse(stop_recorder(sandbox))
        self.assertFalse(recorder_running(sandbox))

    def test_stop_signals_before_it_kills(self) -> None:
        # ffmpeg must get a signal it can act on, or the last fragment of the
        # video is truncated.
        sandbox = FakeSandbox(name="cursor-x")
        sandbox.processes[RECORDER_PROCESS_NAME] = FakeProcess("bash", [], name=RECORDER_PROCESS_NAME)

        def exits_on_signal(command, args=None, **kwargs):
            sandbox.processes.pop(RECORDER_PROCESS_NAME, None)
            return FakeResult(0, "")

        with mock.patch.object(sandbox, "run", side_effect=exits_on_signal):
            self.assertTrue(stop_recorder(sandbox, sleep=lambda _: None))
        self.assertFalse(recorder_running(sandbox))

    def test_stop_falls_back_to_killing_a_recorder_that_ignores_the_signal(self) -> None:
        sandbox = FakeSandbox(name="cursor-x")
        sandbox.processes[RECORDER_PROCESS_NAME] = FakeProcess("bash", [], name=RECORDER_PROCESS_NAME)
        self.assertTrue(stop_recorder(sandbox, sleep=lambda _: None))
        self.assertFalse(recorder_running(sandbox))


class ArtifactTests(unittest.TestCase):
    def test_listing_an_empty_dir(self) -> None:
        sandbox = FakeSandbox(name="cursor-x")
        sandbox.script_result("demo-artifacts", FakeResult(0, ""))
        self.assertEqual(list_artifacts(sandbox), [])

    def test_listing_names(self) -> None:
        sandbox = FakeSandbox(name="cursor-x")
        sandbox.script_result("demo-artifacts", FakeResult(0, "session.mp4\nstill-001.png\n"))
        self.assertEqual(list_artifacts(sandbox), ["session.mp4", "still-001.png"])


class ParserTests(unittest.TestCase):
    def test_collect_defaults_to_a_local_directory(self) -> None:
        args = build_parser().parse_args(["collect"])
        self.assertEqual(Path(args.out), Path("demo-artifacts"))
        self.assertFalse(args.stop)

    def test_watch_takes_a_frame_rate(self) -> None:
        args = build_parser().parse_args(["watch", "--fps", "15"])
        self.assertEqual(args.fps, 15)

    def test_watch_can_name_a_sandbox_directly(self) -> None:
        args = build_parser().parse_args(["watch", "--sandbox", "cursor-tl-mm-tl-demo"])
        self.assertEqual(args.sandbox, "cursor-tl-mm-tl-demo")

    def test_watch_waits_by_default(self) -> None:
        self.assertIsNone(build_parser().parse_args(["watch"]).sandbox)


if __name__ == "__main__":
    unittest.main()
