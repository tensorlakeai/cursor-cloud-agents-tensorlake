"""Run the orchestrator inside a Tensorlake sandbox.

Gets-or-creates one long-lived named sandbox from the orchestrator image and
starts ``cursor-tl-orchestrator`` in it as a managed process that restarts on
failure. Nothing runs on your machine afterwards.

    cursor-tl-orchestrator-sandbox              # launch / resume + ensure running
    cursor-tl-orchestrator-sandbox --restart    # restart the process with the current .env
    cursor-tl-orchestrator-sandbox --status     # print status.json
    cursor-tl-orchestrator-sandbox --logs       # tail the orchestrator log
    cursor-tl-orchestrator-sandbox --terminate  # tear the orchestrator sandbox down

The command is idempotent. Schedule it on a cron so that, if Tensorlake ever
suspends the orchestrator sandbox at your plan's idle ceiling, the next run
resumes it and re-ensures the process.

The orchestrator reads its settings once, at process start. The heartbeat
(status.json) echoes the pool settings it runs with, and every run compares
them with the current ``.env``. When they differ, the process is restarted so
a change to ``CURSOR_POOL_MODE`` or ``CURSOR_POOL_REPO_URL`` takes effect.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Callable, Sequence

# Load .env before importing the SDK: tensorlake.sandbox snapshots
# TENSORLAKE_API_KEY into module-level defaults at import time.
from .config import (
    SANDBOX_HOME,
    SANDBOX_USER_SPEC,
    Config,
    ConfigError,
    heartbeat_settings,
    load_dotenv_if_available,
)

load_dotenv_if_available()

from tensorlake.sandbox import RestartPolicy, RestartPolicyConfig  # noqa: E402

from .orchestrator_image import orchestrator_image_name  # noqa: E402
from .sandbox import GONE_STATUSES, connect_sandbox, orchestrator_sandbox_name, sandbox_status  # noqa: E402

ORCHESTRATOR_PROCESS_NAME = "cursor-tl-orchestrator"
ORCH_STATE_DIR = f"{SANDBOX_HOME}/.cursor-tensorlake"
ORCH_LOG = f"{ORCH_STATE_DIR}/orchestrator.log"
ORCH_STATUS = f"{ORCH_STATE_DIR}/status.json"

# Env forwarded from the launcher into the in-sandbox orchestrator. STATE_DIR is
# pinned so --status and --logs know where to look. CURSOR_API_URL is for the
# orchestrator's REST calls; the spawn hook never passes it to a worker.
_FORWARDED_ENV = (
    "CURSOR_API_KEY",
    "CURSOR_POOL",
    "CURSOR_POOL_REPO_URL",
    "CURSOR_POOL_MODE",
    "CURSOR_API_URL",
    "TENSORLAKE_API_KEY",
    "TENSORLAKE_ORGANIZATION_ID",
    "TENSORLAKE_PROJECT_ID",
    "TENSORLAKE_NAMESPACE",
    "IMAGE_NAME",
    "SANDBOX_CPUS",
    "SANDBOX_MEMORY_MB",
    "SANDBOX_DISK_MB",
    "SANDBOX_TIMEOUT_SECS",
    "SANDBOX_ALLOW_OUT",
    "SANDBOX_LAUNCH_TIMEOUT_SECS",
    "WORKER_IDLE_RELEASE_SECS",
    "WORKER_LABELS_JSON",
    "WORKER_COMPUTER_USE",
    "WORKER_SHARE_DESKTOP",
    "WORKER_DISPLAY",
    "WARM_IDLE",
    "MAX_WORKERS",
    "SESSION_RETENTION_SECS",
    "JANITOR_INTERVAL_SECS",
    "WAKE_OFFLINE_CLAIMS",
    "WAKE_POLL_SECS",
    "HTTPS_PROXY",
    "NO_PROXY",
    "LOG_LEVEL",
)


def forwarded_env() -> dict[str, str]:
    env = {name: os.environ[name] for name in _FORWARDED_ENV if os.environ.get(name, "").strip()}
    env["STATE_DIR"] = ORCH_STATE_DIR
    env["HOME"] = SANDBOX_HOME
    env["PATH"] = "/usr/local/bin:/usr/bin:/bin"
    return env


def orchestrator_running(sandbox: Any) -> bool:
    try:
        proc = sandbox.get_process(ORCHESTRATOR_PROCESS_NAME)
    except Exception:
        return False
    status = getattr(proc, "status", "")
    return str(getattr(status, "value", status) or "").lower() == "running"


def _status_in(sandbox: Any) -> dict[str, Any] | None:
    """status.json from a running sandbox, or None when there is none yet."""
    result = sandbox.run("sh", ["-c", f"cat {ORCH_STATUS} 2>/dev/null"], timeout=30)
    text = (getattr(result, "stdout", "") or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def pool_settings(config: Config) -> dict[str, Any]:
    """The settings the orchestrator echoes in its heartbeat."""
    return dict(heartbeat_settings(config))


def settings_differ(status: dict[str, Any] | None, config: Config) -> bool:
    """True when a running orchestrator's heartbeat does not match ``.env``.

    No heartbeat yet means the process is still starting: not a difference.
    A heartbeat without the settings comes from an older orchestrator that
    cannot say what it runs with, so it counts as different.
    """
    if status is None:
        return False
    return any(status.get(key) != value for key, value in pool_settings(config).items())


TERMINATE_WAIT_SECS = 120.0


def _terminate_and_wait(
    config: Config,
    name: str,
    *,
    timeout_secs: float = TERMINATE_WAIT_SECS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Terminate the sandbox that holds ``name`` and wait until the name is free.

    Termination is asynchronous. ``get_or_create`` would otherwise attach to
    the sandbox while it is still shutting down, and the next call on it fails
    with "not found or not running". Returns False when there was nothing to
    terminate.
    """
    old = connect_sandbox(config, name)
    if old is None:
        return False
    old.terminate()
    deadline = clock() + timeout_secs
    while clock() < deadline:
        current = connect_sandbox(config, name)
        if current is None or sandbox_status(current) in GONE_STATUSES:
            return True
        sleep(2.0)
    raise RuntimeError(f"sandbox {name} did not terminate within {int(timeout_secs)}s")


_OUTCOME_TEXT = {
    "created": "Created a new sandbox from image",
    "resumed": "Resumed the existing sandbox; image",
    "attached": "Attached to the running sandbox; image",
}


def ensure_orchestrator(
    config: Config, *, indent: str = "", restart: bool = False, recreate: bool = False
) -> Any:
    """Create or resume the orchestrator sandbox and start the orchestrator in it.

    ``indent`` prefixes every progress line, so a caller that prints its own
    step header can nest these lines under it. ``restart`` replaces a running
    orchestrator process; without it the process is replaced only when its
    heartbeat shows pool settings that differ from ``.env``. ``recreate``
    terminates an existing sandbox first, so the new one runs the current
    orchestrator image. A sandbox keeps the image it was created from.
    """
    from tensorlake.sandbox import Sandbox

    def note(text: str) -> None:
        print(f"{indent}{text}", flush=True)

    name = orchestrator_sandbox_name(config.cursor_pool)
    image = orchestrator_image_name()
    if recreate and _terminate_and_wait(config, name):
        note(f"Terminated the old sandbox {name}; the new one runs the rebuilt image.")
    sandbox = Sandbox.get_or_create(
        name,
        image=image,
        cpus=config.orchestrator_cpus,
        memory_mb=config.orchestrator_memory_mb,
        disk_mb=config.orchestrator_disk_mb,
        timeout_secs=0,  # plan maximum idle window; the cron covers the rest
        allow_internet_access=True,
        **config.tensorlake_kwargs(),
    )
    outcome = str(getattr(sandbox, "bind_outcome", "attached"))
    note(f"Sandbox: {name} (id {sandbox.sandbox_id})")
    note(f"{_OUTCOME_TEXT.get(outcome, outcome.capitalize() + '; image')} {image}")

    if orchestrator_running(sandbox):
        if restart:
            note("Orchestrator process: restarting (--restart)...")
        elif settings_differ(_status_in(sandbox), config):
            note("Orchestrator process: pool settings changed in .env; restarting...")
        else:
            note("Orchestrator process: already running with the current .env settings.")
            return sandbox
        try:
            sandbox.kill_process(ORCHESTRATOR_PROCESS_NAME)
        except Exception:
            pass
    else:
        note("Orchestrator process: starting...")
    start = f"mkdir -p {ORCH_STATE_DIR} && exec cursor-tl-orchestrator >> {ORCH_LOG} 2>&1"
    try:
        sandbox.start_process(
            "bash",
            ["-c", start],
            env=forwarded_env(),
            user=SANDBOX_USER_SPEC,
            name=ORCHESTRATOR_PROCESS_NAME,
            restart=RestartPolicyConfig(policy=RestartPolicy.ALWAYS, initial_backoff_ms=2000),
        )
    except Exception:
        # A finished managed process may still hold the name.
        try:
            sandbox.kill_process(ORCHESTRATOR_PROCESS_NAME)
        except Exception:
            pass
        sandbox.start_process(
            "bash",
            ["-c", start],
            env=forwarded_env(),
            user=SANDBOX_USER_SPEC,
            name=ORCHESTRATOR_PROCESS_NAME,
            restart=RestartPolicyConfig(policy=RestartPolicy.ALWAYS, initial_backoff_ms=2000),
        )
    note("Orchestrator process: started. It restarts on its own if it exits.")
    return sandbox


def read_status(config: Config) -> dict[str, Any] | None:
    """The orchestrator heartbeat, or None when there is no sandbox or no file yet."""
    name = orchestrator_sandbox_name(config.cursor_pool)
    sandbox = connect_sandbox(config, name)
    if sandbox is None:
        return None
    if sandbox_status(sandbox) in {"suspended", "suspending"}:
        sandbox.resume()
    return _status_in(sandbox)


def print_status(config: Config) -> int:
    name = orchestrator_sandbox_name(config.cursor_pool)
    sandbox = connect_sandbox(config, name)
    if sandbox is None:
        print(f"No orchestrator sandbox found ({name}).")
        return 1
    print(f"Orchestrator sandbox: {name} ({sandbox.sandbox_id}) status={sandbox_status(sandbox)}")
    if sandbox_status(sandbox) in {"suspended", "suspending"}:
        print("(sandbox is suspended; showing the last heartbeat)")
    status = read_status(config)
    if status is None:
        print("No status.json yet; the orchestrator may still be starting.")
    else:
        print(json.dumps(status, indent=2, sort_keys=True))
    return 0


def print_logs(config: Config, lines: int) -> int:
    name = orchestrator_sandbox_name(config.cursor_pool)
    sandbox = connect_sandbox(config, name)
    if sandbox is None:
        print(f"No orchestrator sandbox found ({name}).")
        return 1
    if sandbox_status(sandbox) in {"suspended", "suspending"}:
        sandbox.resume()
    result = sandbox.run("sh", ["-c", f"tail -n {int(lines)} {ORCH_LOG} 2>/dev/null || true"], timeout=30)
    sys.stdout.write(getattr(result, "stdout", "") or "")
    return 0


def terminate(config: Config) -> int:
    name = orchestrator_sandbox_name(config.cursor_pool)
    sandbox = connect_sandbox(config, name)
    if sandbox is None:
        print(f"No orchestrator sandbox found ({name}).")
        return 0
    sandbox.terminate()
    print(f"Terminated orchestrator sandbox {name}.")
    print("Worker sandboxes are left in place. List them with: tl sbx ls")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cursor-tl-orchestrator-sandbox",
        description="Run the Cursor worker orchestrator inside a Tensorlake sandbox.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="print the orchestrator status")
    group.add_argument("--logs", action="store_true", help="tail the orchestrator log")
    group.add_argument("--terminate", action="store_true", help="terminate the orchestrator sandbox")
    group.add_argument(
        "--restart",
        action="store_true",
        help="restart the orchestrator process so it reads the current .env",
    )
    parser.add_argument("--lines", type=int, default=200, help="log lines for --logs")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        config = Config.from_env(require_image=not (args.status or args.logs or args.terminate))
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    try:
        if args.status:
            return print_status(config)
        if args.logs:
            return print_logs(config, args.lines)
        if args.terminate:
            return terminate(config)
        ensure_orchestrator(config, restart=args.restart)
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
