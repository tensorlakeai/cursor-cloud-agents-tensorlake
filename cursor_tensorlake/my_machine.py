"""My Machines quickstart: one personal Cursor worker in one Tensorlake sandbox.

No Enterprise plan or pool needed. The sandbox is named, so ``--suspend`` and
``--resume`` hibernate and wake it with the checkout intact.

    cursor-tl-my-machine --name tl-demo            # create / resume, start the worker
    cursor-tl-my-machine --name tl-demo --status
    cursor-tl-my-machine --name tl-demo --logs
    cursor-tl-my-machine --name tl-demo --suspend
    cursor-tl-my-machine --name tl-demo --terminate

Set ``CURSOR_USER_API_KEY`` (a personal key from cursor.com/dashboard) and
``REPOS`` (HTTPS URLs). My Machines routes a chat to the machine whose worker
directory has the repository as a git remote, so the repos are cloned first.
Then pick the machine from the environment dropdown at cursor.com/agents.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from typing import Any, Sequence

from .config import (
    AGENT_BIN,
    SANDBOX_HOME,
    SANDBOX_USER_SPEC,
    WORKER_LOG_PATH,
    WORKER_PROCESS_NAME,
    WORKSPACE_DIR,
    Config,
    ConfigError,
    computer_use_flags,
    load_dotenv_if_available,
)

load_dotenv_if_available()

from tensorlake.sandbox import RestartPolicy, RestartPolicyConfig, Sandbox  # noqa: E402

from .sandbox import connect_sandbox, my_machine_sandbox_name, process_status, sandbox_status  # noqa: E402

ASKPASS_PATH = f"{SANDBOX_HOME}/.cursor-tl-askpass.sh"
ASKPASS_SCRIPT = (
    "#!/bin/sh\n"
    'case "$1" in\n'
    '  Username*) printf "%s\\n" "$GIT_ASKPASS_USER" ;;\n'
    '  *) printf "%s\\n" "$GIT_ASKPASS_PASS" ;;\n'
    "esac\n"
)


def clone_repos(config: Config, sandbox: Any) -> list[str]:
    """Clone ``REPOS`` into the workspace. Returns the worker directories."""
    dirs: list[str] = []
    env = {
        "HOME": SANDBOX_HOME,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "GIT_TERMINAL_PROMPT": "0",
    }
    if config.git_token:
        sandbox.write_file(ASKPASS_PATH, ASKPASS_SCRIPT.encode("utf-8"))
        sandbox.run("chmod", ["0755", ASKPASS_PATH], user=SANDBOX_USER_SPEC, timeout=30)
        env.update(
            {
                "GIT_ASKPASS": ASKPASS_PATH,
                "GIT_ASKPASS_USER": config.git_username or "x-access-token",
                "GIT_ASKPASS_PASS": config.git_token,
            }
        )
    for repo in config.repos:
        dest = f"{WORKSPACE_DIR}/{repo.target_name}"
        script = (
            f"if [ -d {shlex.quote(dest)}/.git ]; then git -C {shlex.quote(dest)} fetch --all --prune -q; "
            f"else git clone -q {shlex.quote(repo.url)} {shlex.quote(dest)}; fi"
        )
        result = sandbox.run("bash", ["-c", script], env=env, user=SANDBOX_USER_SPEC, timeout=600)
        if getattr(result, "exit_code", 1) != 0:
            raise RuntimeError(
                f"clone of {repo.url} failed: " + config.redacted((result.stderr or result.stdout or "").strip()[:400])
            )
        dirs.append(dest)
    return dirs


def worker_running(sandbox: Any) -> bool:
    try:
        return process_status(sandbox.get_process(WORKER_PROCESS_NAME)) == "running"
    except Exception:
        return False


def machine_worker_command(config: Config, machine_name: str, dirs: Sequence[str]) -> list[str]:
    """The ``agent worker`` argv for one My Machines worker."""
    command = [AGENT_BIN, "worker", "--name", machine_name, "--management-addr", "127.0.0.1:8080"]
    for directory in dirs:
        command.extend(["--worker-dir", directory])
    command.extend(computer_use_flags(config))
    command.extend(["start", "--verbose"])
    return command


def ensure_machine(config: Config, machine_name: str) -> Any:
    if not config.cursor_user_api_key:
        raise ConfigError("CURSOR_USER_API_KEY is required for My Machines")
    if not config.repos:
        raise ConfigError("REPOS is required: My Machines routes by the repository remote")

    name = my_machine_sandbox_name(machine_name)
    sandbox = Sandbox.get_or_create(
        name,
        image=config.image_name,
        cpus=config.sandbox_cpus,
        memory_mb=config.sandbox_memory_mb,
        disk_mb=config.sandbox_disk_mb,
        timeout_secs=config.sandbox_timeout_secs,
        allow_internet_access=True,
        allow_out=list(config.sandbox_allow_out) or None,
        **config.tensorlake_kwargs(),
    )
    print(f"Machine sandbox: {name} ({sandbox.sandbox_id}) {getattr(sandbox, 'bind_outcome', 'attached')}")

    if worker_running(sandbox):
        print("Worker already running.")
        return sandbox

    dirs = clone_repos(config, sandbox)
    command = machine_worker_command(config, machine_name, dirs)
    script = f"exec {shlex.join(command)} >> {shlex.quote(WORKER_LOG_PATH)} 2>&1"
    env = {
        "CURSOR_API_KEY": config.cursor_user_api_key,
        "HOME": SANDBOX_HOME,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "GIT_TERMINAL_PROMPT": "0",
        "NODE_COMPILE_CACHE": "/tmp/cursor-compile-cache",
    }
    try:
        sandbox.kill_process(WORKER_PROCESS_NAME)
    except Exception:
        pass
    sandbox.start_process(
        "bash",
        ["-c", script],
        env=env,
        working_dir=WORKSPACE_DIR,
        user=SANDBOX_USER_SPEC,
        name=WORKER_PROCESS_NAME,
        restart=RestartPolicyConfig(policy=RestartPolicy.ON_FAILURE, max_restarts=5),
    )
    print(f"Worker started as machine {machine_name!r}. Pick it at https://cursor.com/agents")
    return sandbox


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cursor-tl-my-machine", description=__doc__.split("\n\n")[0])
    parser.add_argument("--name", required=True, help="machine name shown in Cursor")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true")
    group.add_argument("--logs", action="store_true")
    group.add_argument("--suspend", action="store_true", help="hibernate the machine")
    group.add_argument("--terminate", action="store_true")
    parser.add_argument("--lines", type=int, default=200)
    args = parser.parse_args(argv)

    try:
        config = Config.from_env(
            require_image=not (args.status or args.logs or args.suspend or args.terminate),
            require_cursor_key=False,
        )
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    name = my_machine_sandbox_name(args.name)

    if args.status or args.logs or args.suspend or args.terminate:
        sandbox = connect_sandbox(config, name)
        if sandbox is None:
            print(f"No machine sandbox found ({name}).")
            return 1
        if args.status:
            status = sandbox_status(sandbox)
            running = worker_running(sandbox) if status == "running" else False
            print(f"{name} ({sandbox.sandbox_id}) status={status} worker_running={running}")
            return 0
        if args.logs:
            if sandbox_status(sandbox) in {"suspended", "suspending"}:
                sandbox.resume()
            result = sandbox.run("sh", ["-c", f"tail -n {int(args.lines)} {WORKER_LOG_PATH} 2>/dev/null || true"], timeout=30)
            sys.stdout.write(getattr(result, "stdout", "") or "")
            return 0
        if args.suspend:
            sandbox.suspend()
            print(f"Suspended {name}. Run again without flags to resume and restart the worker.")
            return 0
        sandbox.terminate()
        print(f"Terminated {name}.")
        return 0

    try:
        ensure_machine(config, args.name)
    except (ConfigError, RuntimeError) as exc:
        print(f"Error: {config.redacted(exc)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
