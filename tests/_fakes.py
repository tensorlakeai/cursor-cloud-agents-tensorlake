"""Fakes for the Tensorlake SDK surface this package touches. No network."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any


class FakeResult:
    def __init__(self, exit_code: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class FakeProcess:
    def __init__(self, command: str, args: list[str], name: str | None = None, status: str = "running") -> None:
        self.command = command
        self.args = args
        self.name = name
        self.status = status
        self.exit_code: int | None = None
        self.pid = 4242


class NotFound(Exception):
    status_code = 404


class FakeSandbox:
    def __init__(self, sandbox_id: str = "sbx_1", name: str = "cursor-w1", status: str = "running", bind_outcome: str = "created") -> None:
        self.sandbox_id = sandbox_id
        self.name = name
        self.status = status
        self.bind_outcome = bind_outcome
        self.runs: list[tuple[str, list[str], dict[str, Any]]] = []
        self.started: list[dict[str, Any]] = []
        self.written: dict[str, bytes] = {}
        self.processes: dict[str, FakeProcess] = {}
        self.suspended = False
        self.resumed = False
        self.terminated = False
        self.default_result = FakeResult(0, "")
        self._scripted: dict[str, FakeResult] = {}
        self.start_process_error: Exception | None = None
        self.exit_after_start: str | None = None  # status to report right after start

    def script_result(self, needle: str, result: FakeResult) -> None:
        self._scripted[needle] = result

    def run(self, command: str, args=None, **kwargs: Any) -> FakeResult:
        args = list(args or [])
        self.runs.append((command, args, kwargs))
        blob = command + " " + " ".join(args)
        for needle, result in self._scripted.items():
            if needle in blob:
                return result
        return self.default_result

    def start_process(self, command: str, args=None, **kwargs: Any) -> FakeProcess:
        if self.start_process_error is not None:
            error, self.start_process_error = self.start_process_error, None
            raise error
        name = kwargs.get("name")
        if name in self.processes and self.processes[name].status == "running":
            raise RuntimeError(f"process name {name!r} already in use")
        proc = FakeProcess(command, list(args or []), name=name)
        if self.exit_after_start:
            proc.status = self.exit_after_start
            proc.exit_code = 1
        self.started.append({"command": command, "args": list(args or []), **kwargs})
        if name:
            self.processes[name] = proc
        return proc

    def get_process(self, process: Any = None, **_: Any) -> FakeProcess:
        proc = self.processes.get(str(process))
        if proc is None:
            raise NotFound(f"no process {process}")
        return proc

    def kill_process(self, process: Any = None, **_: Any) -> None:
        self.processes.pop(str(process), None)

    def list_processes(self):
        return list(self.processes.values())

    def write_file(self, path: str, content: bytes) -> None:
        self.written[path] = content

    def read_file(self, path: str) -> bytes:
        return self.written.get(path, b"")

    def suspend(self, **_: Any) -> None:
        self.suspended = True
        self.status = "suspended"

    def resume(self, **_: Any) -> None:
        self.resumed = True
        self.status = "running"

    def terminate(self, **_: Any) -> None:
        self.terminated = True
        self.status = "terminated"


class FakeInfo:
    def __init__(self, sandbox_id: str, name: str, status: str = "running") -> None:
        self.sandbox_id = sandbox_id
        self.name = name
        self.status = status


def make_config(*, from_env_kwargs: dict[str, Any] | None = None, **overrides: Any):
    """Build a real Config via from_env with dummy secrets and a temp state dir."""
    from cursor_tensorlake.config import Config

    env = {
        "CURSOR_API_KEY": "sa_dummy_cursor_key",
        "TENSORLAKE_API_KEY": "tl_dummy_key",
        "IMAGE_NAME": "cursor-tl-worker-abc12345",
        "CURSOR_POOL": "tensorlake",
    }
    env.update({k: str(v) for k, v in overrides.items()})
    state_dir = env.get("STATE_DIR") or tempfile.mkdtemp(prefix="cursor-tl-state-")
    env["STATE_DIR"] = state_dir
    saved = dict(os.environ)
    try:
        os.environ.update(env)
        return Config.from_env(**(from_env_kwargs or {})), Path(state_dir)
    finally:
        os.environ.clear()
        os.environ.update(saved)


def make_claim(**overrides: Any):
    from cursor_tensorlake.config import Claim

    env = {
        "CURSOR_AGENT_WORKER_ID": "worker-abc123",
        "CURSOR_POOL": "tensorlake",
        "CURSOR_REQUEST_ID": "bc-00000000-0000-0000-0000-000000000002",
        "CURSOR_REPO_URL": "github.com/acme/widgets",
        "CURSOR_WORKER_NAME": "tl-worker",
        "CURSOR_API_URL": "https://api.cursor.com",
        "CURSOR_API_ENDPOINT": "https://api.cursor.com",
    }
    env.update({k: str(v) for k, v in overrides.items()})
    return Claim.from_env(env)
