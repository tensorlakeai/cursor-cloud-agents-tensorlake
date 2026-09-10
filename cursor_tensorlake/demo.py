"""Record what a Cursor agent does on a worker sandbox's desktop.

Cursor's ``--computer-use`` sends its screenshots to the model, not to disk, so
a run that clicks around a web app leaves nothing to show afterwards. This
command films the desktop from outside the agent: it starts an ffmpeg screen
recorder in the worker sandbox, then copies the video and the stills out.

It drives nothing. The agent opens the browser, moves the pointer, and clicks,
which is the point being demonstrated: Cursor's own computer use working while
its worker runs in a Tensorlake sandbox.

    cursor-tl-demo watch      # wait for a worker sandbox, start recording
    cursor-tl-demo collect    # copy the video and stills to ./demo-artifacts
    cursor-tl-demo stop       # stop recording, keep the sandbox
    cursor-tl-demo status     # sandboxes, recorder, artifacts so far
"""

from __future__ import annotations

import argparse
import logging
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from .config import (
    SANDBOX_HOME,
    SANDBOX_USER_SPEC,
    Config,
    ConfigError,
    load_dotenv_if_available,
)
from .sandbox import (
    SLEEPING_STATUSES,
    connect_sandbox,
    list_session_sandboxes,
    process_status,
    sandbox_status,
)

LOGGER = logging.getLogger(__name__)

RECORDER_PROCESS_NAME = "cursor-tl-demo-recorder"
RECORDER_PATH = "/usr/local/bin/cursor-tl-demo-recorder"
ARTIFACT_DIR = f"{SANDBOX_HOME}/demo-artifacts"
LOCAL_ARTIFACT_DIR = Path("demo-artifacts")
RECORDER_SOURCE = Path(__file__).resolve().parent / "demo_recorder.sh"

WATCH_POLL_SECS = 3.0
DEFAULT_WATCH_TIMEOUT_SECS = 900.0


# --- finding the worker -----------------------------------------------------


def newest_worker(infos: Sequence[Any], *, running_only: bool = False) -> Any | None:
    """The worker sandbox a recorder should attach to.

    A running sandbox always wins over a suspended one: a suspended worker has
    no desktop to film. Among equals the most recently created one wins, which
    is the request just sent.

    ``running_only`` drops suspended sandboxes rather than falling back to
    them, so waiting for "the worker for the request I just sent" does not
    settle for a leftover from an earlier one.
    """
    live = [info for info in infos if sandbox_status(info) not in SLEEPING_STATUSES]
    pool = live if running_only else (live or list(infos))
    if not pool:
        return None
    return max(pool, key=lambda info: (getattr(info, "created_at", None) or 0, getattr(info, "name", "")))


def wait_for_worker(
    config: Config,
    *,
    timeout_secs: float = DEFAULT_WATCH_TIMEOUT_SECS,
    sleep: Any = time.sleep,
    clock: Any = time.monotonic,
) -> Any:
    """Block until a running worker sandbox exists, then return its info."""
    deadline = clock() + timeout_secs
    announced = False
    while True:
        worker = newest_worker(list_session_sandboxes(config), running_only=True)
        if worker is not None:
            return worker
        if not announced:
            print("Waiting for Cursor to claim a request and start a worker sandbox...")
            announced = True
        if clock() >= deadline:
            raise ConfigError(
                f"no worker sandbox appeared within {int(timeout_secs)}s; "
                "check `cursor-tl-orchestrator-sandbox --logs` and `cursor-tl-pool pending`"
            )
        sleep(WATCH_POLL_SECS)


# --- recording --------------------------------------------------------------


def recorder_running(sandbox: Any) -> bool:
    try:
        proc = sandbox.get_process(RECORDER_PROCESS_NAME)
    except Exception:
        return False
    return process_status(proc) == "running"


def install_recorder(sandbox: Any) -> None:
    sandbox.write_file(RECORDER_PATH, RECORDER_SOURCE.read_bytes())
    sandbox.run("sh", ["-c", f"chmod 0755 {shlex.quote(RECORDER_PATH)}"], timeout=30)


def start_recorder(sandbox: Any, *, display: str | None, fps: int) -> None:
    """Start the recorder as a named process, as the user that owns the desktop."""
    command = f"{shlex.quote(RECORDER_PATH)} {shlex.quote(ARTIFACT_DIR)}"
    if display:
        command += f" {shlex.quote(display)}"
    sandbox.start_process(
        "bash",
        ["-c", f"exec {command} >> /tmp/cursor-tl-demo-recorder.log 2>&1"],
        env={
            "HOME": SANDBOX_HOME,
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "RECORD_FPS": str(fps),
        },
        user=SANDBOX_USER_SPEC,
        name=RECORDER_PROCESS_NAME,
        restart={"policy": "never"},
    )


PID_FILE = f"{ARTIFACT_DIR}/recorder.pid"
STOP_WAIT_SECS = 12.0


def stop_recorder(sandbox: Any, *, sleep: Any = time.sleep) -> bool:
    """Stop the recorder, giving ffmpeg the chance to close its file.

    Killing the managed process outright leaves the last fragment of the video
    truncated. Signalling the script first lets its trap send ffmpeg a SIGINT,
    so the recording ends on a whole frame.
    """
    if not recorder_running(sandbox):
        return False
    sandbox.run(
        "sh",
        ["-c", f'test -f {shlex.quote(PID_FILE)} && kill -TERM "$(cat {shlex.quote(PID_FILE)})"'],
        timeout=30,
    )
    waited = 0.0
    while waited < STOP_WAIT_SECS:
        if not recorder_running(sandbox):
            return True
        sleep(1.0)
        waited += 1.0
    # It ignored the signal; take the managed process down.
    sandbox.kill_process(RECORDER_PROCESS_NAME)
    return True


# --- collecting -------------------------------------------------------------


def tl_cli_env() -> dict[str, str]:
    """`tl` reads the key from the shell, and .env is what the operator edited."""
    env = dict(os.environ)
    for name in ("TENSORLAKE_API_KEY", "TENSORLAKE_ORGANIZATION_ID", "TENSORLAKE_PROJECT_ID"):
        value = os.environ.get(name, "").strip()
        if value:
            env[name] = value
    return env


def copy_out(sandbox_name: str, remote: str, local: Path) -> bool:
    """Copy one path out with `tl sbx cp`. False when it is not there."""
    result = subprocess.run(
        ["tl", "sbx", "cp", f"{sandbox_name}:{remote}", str(local)],
        capture_output=True,
        text=True,
        env=tl_cli_env(),
    )
    if result.returncode != 0:
        LOGGER.debug("cp %s failed: %s", remote, (result.stderr or "").strip()[:200])
        return False
    return True


def list_artifacts(sandbox: Any) -> list[str]:
    result = sandbox.run(
        "sh", ["-c", f"ls -1 {shlex.quote(ARTIFACT_DIR)} 2>/dev/null"], timeout=30
    )
    return [line.strip() for line in (getattr(result, "stdout", "") or "").splitlines() if line.strip()]


def make_gif(video: Path) -> Path | None:
    """A shareable GIF from the video, when ffmpeg is on the local machine."""
    if not video.is_file():
        return None
    gif = video.with_suffix(".gif")
    palette = video.parent / "palette.png"
    common = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y"]
    scale = "fps=8,scale=900:-1:flags=lanczos"
    try:
        subprocess.run(
            [*common, "-i", str(video), "-vf", f"{scale},palettegen", str(palette)],
            check=True, capture_output=True,
        )
        subprocess.run(
            [*common, "-i", str(video), "-i", str(palette),
             "-lavfi", f"{scale} [x]; [x][1:v] paletteuse", str(gif)],
            check=True, capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        LOGGER.debug("gif conversion skipped: %s", exc.__class__.__name__)
        return None
    finally:
        palette.unlink(missing_ok=True)
    return gif


def collect(sandbox: Any, name: str, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    names = list_artifacts(sandbox)
    if not names:
        print(f"No artifacts in {ARTIFACT_DIR} yet. Is the recorder running?")
        return []
    written: list[Path] = []
    for entry in names:
        if entry == "recorder.pid":
            continue
        local = out_dir / entry
        if copy_out(name, f"{ARTIFACT_DIR}/{entry}", local):
            written.append(local)
            print(f"  {local} ({local.stat().st_size} bytes)")
    video = out_dir / "session.mp4"
    if video in written:
        gif = make_gif(video)
        if gif is not None:
            written.append(gif)
            print(f"  {gif} ({gif.stat().st_size} bytes)")
        else:
            print("  (no GIF: install ffmpeg locally to get one)")
    return written


# --- commands ---------------------------------------------------------------


def resolve_target(config: Config, name: str | None) -> tuple[Any, str]:
    if name:
        sandbox = connect_sandbox(config, name)
        if sandbox is None:
            raise ConfigError(f"no sandbox named {name}")
        return sandbox, name
    worker = newest_worker(list_session_sandboxes(config))
    if worker is None:
        raise ConfigError(
            "no worker sandbox exists. Send a request to the pool first, or run "
            "`cursor-tl-demo watch` to wait for one."
        )
    worker_name = str(getattr(worker, "name", ""))
    sandbox = connect_sandbox(config, worker_name)
    if sandbox is None:
        raise ConfigError(f"could not attach to {worker_name}")
    return sandbox, worker_name


def cmd_watch(config: Config, args: argparse.Namespace) -> int:
    if not config.worker_computer_use:
        print(
            "WORKER_COMPUTER_USE is not true in .env, so workers start without a\n"
            "desktop. Run `cursor-tl-up --computer-use --rebuild` first.",
            file=sys.stderr,
        )
        return 2
    if args.sandbox:
        # A named sandbox, for a My Machines worker or any sandbox that is not
        # a pool worker. `list_session_sandboxes` deliberately skips those.
        sandbox = connect_sandbox(config, args.sandbox)
        if sandbox is None:
            print(f"No sandbox named {args.sandbox}", file=sys.stderr)
            return 1
        name = args.sandbox
        print(f"Sandbox: {name} ({sandbox_status(sandbox)})")
    else:
        info = wait_for_worker(config, timeout_secs=args.timeout)
        name = str(getattr(info, "name", ""))
        print(f"Worker sandbox: {name} ({sandbox_status(info)})")
        sandbox = connect_sandbox(config, name)
        if sandbox is None:
            print(f"Could not attach to {name}", file=sys.stderr)
            return 1
    if recorder_running(sandbox):
        print("A recorder is already running there. Nothing to do.")
        return 0
    install_recorder(sandbox)
    start_recorder(sandbox, display=config.worker_display, fps=args.fps)
    where = config.worker_display or "the display with a browser window"
    print(f"Recording {where} into {ARTIFACT_DIR}.")
    print("Let the agent work, then run `cursor-tl-demo collect`.")
    return 0


def cmd_stop(config: Config, args: argparse.Namespace) -> int:
    sandbox, name = resolve_target(config, args.sandbox)
    print("Recorder stopped." if stop_recorder(sandbox) else "No recorder was running.")
    print(f"Sandbox {name} is untouched.")
    return 0


def cmd_collect(config: Config, args: argparse.Namespace) -> int:
    sandbox, name = resolve_target(config, args.sandbox)
    out_dir = Path(args.out)
    if args.stop:
        stop_recorder(sandbox)
    print(f"Collecting from {name} into {out_dir}/")
    written = collect(sandbox, name, out_dir)
    if not written:
        return 1
    print(f"\n{len(written)} file(s). The video is {out_dir}/session.mp4.")
    return 0


def cmd_status(config: Config, _args: argparse.Namespace) -> int:
    infos = list_session_sandboxes(config)
    if not infos:
        print("No worker sandboxes.")
        return 0
    print(f"Worker sandboxes ({len(infos)}):")
    for info in infos:
        print(f"  {getattr(info, 'name', '?')}  {sandbox_status(info)}")
    worker = newest_worker(infos)
    name = str(getattr(worker, "name", ""))
    sandbox = connect_sandbox(config, name)
    if sandbox is None or sandbox_status(worker) in SLEEPING_STATUSES:
        print(f"\n{name} is not running, so there is nothing to record.")
        return 0
    print(f"\n{name}:")
    print(f"  recorder: {'running' if recorder_running(sandbox) else 'not running'}")
    names = list_artifacts(sandbox)
    print(f"  artifacts: {', '.join(names) if names else 'none yet'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cursor-tl-demo",
        description="Record a Cursor agent driving the desktop of its Tensorlake worker sandbox.",
    )
    sub = parser.add_subparsers(dest="command")

    watch = sub.add_parser("watch", help="wait for a worker sandbox and start recording")
    watch.add_argument("--sandbox", help="record this sandbox now instead of waiting for a pool worker")
    watch.add_argument("--timeout", type=float, default=DEFAULT_WATCH_TIMEOUT_SECS,
                       help="seconds to wait for a worker sandbox (default 900)")
    watch.add_argument("--fps", type=int, default=10, help="video frame rate (default 10)")
    watch.set_defaults(func=cmd_watch)

    collect_cmd = sub.add_parser("collect", help="copy the video and stills out of the worker")
    collect_cmd.add_argument("--sandbox", help="sandbox name; default is the newest worker")
    collect_cmd.add_argument("--out", default=str(LOCAL_ARTIFACT_DIR), help="local directory")
    collect_cmd.add_argument("--stop", action="store_true", help="stop the recorder first")
    collect_cmd.set_defaults(func=cmd_collect)

    stop = sub.add_parser("stop", help="stop the recorder, leave the sandbox alone")
    stop.add_argument("--sandbox", help="sandbox name; default is the newest worker")
    stop.set_defaults(func=cmd_stop)

    status = sub.add_parser("status", help="worker sandboxes, recorder state, artifacts")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s cursor-tl-demo %(message)s",
    )
    load_dotenv_if_available()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"cursor-tl-demo: {exc}", file=sys.stderr)
        return 2
    try:
        return int(args.func(config, args))
    except ConfigError as exc:
        print(f"cursor-tl-demo: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
