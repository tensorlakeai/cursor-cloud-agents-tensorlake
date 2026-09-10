"""One command from API keys to a pool that accepts Cursor requests.

``cursor-tl-up`` does, in order:

1. read ``.env``; ask for any missing key and save it (``--non-interactive``
   fails instead of asking);
2. check the Cursor service-account key and register the pool if it is new;
3. find or build the worker image and pin its name in ``.env``;
4. find or build the orchestrator image (``--rebuild`` forces a build);
5. create or resume the orchestrator sandbox and start the orchestrator;
6. wait until the heartbeat reports ``watching`` and print how to send a task.

Every step is idempotent, so the command is also the repair command.
"""

from __future__ import annotations

import argparse
import getpass
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from .config import DEFAULT_CURSOR_POOL, Config, ConfigError, load_dotenv_if_available
from .cursor_api import CursorAPI, CursorAPIError

ENV_PATH = Path(".env")
REQUIRED_SECRETS = ("CURSOR_API_KEY", "TENSORLAKE_API_KEY")
WATCH_TIMEOUT_SECS = 90.0
STEP_INDENT = "      "  # detail lines sit under their "[n/6]" header


# --- .env handling ----------------------------------------------------------


def update_env_file(path: Path, updates: Mapping[str, str]) -> None:
    """Set ``KEY=value`` lines in a dotenv file. Comments and order survive."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else None
        if key in pending:
            out.append(f"{key}={pending.pop(key)}")
        else:
            out.append(line)
    for key, value in pending.items():
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def collect_missing(
    env: Mapping[str, str],
    *,
    interactive: bool,
    ask_secret: Callable[[str], str] = getpass.getpass,
    ask: Callable[[str], str] = input,
) -> dict[str, str]:
    """Return the values to add to ``.env``. Ask when a terminal is available."""
    updates: dict[str, str] = {}
    prompts = {
        "CURSOR_API_KEY": "Cursor service-account API key: ",
        "TENSORLAKE_API_KEY": "Tensorlake API key: ",
    }
    for name in REQUIRED_SECRETS:
        if env.get(name, "").strip() and not env[name].startswith("replace-with"):
            continue
        if not interactive:
            raise ConfigError(f"{name} is not set. Add it to .env or run without --non-interactive.")
        value = ask_secret(prompts[name]).strip()
        if not value:
            raise ConfigError(f"{name} is required.")
        updates[name] = value
    if not env.get("CURSOR_POOL", "").strip():
        if interactive:
            answer = ask(f"Cursor pool name [{DEFAULT_CURSOR_POOL}]: ").strip()
            updates["CURSOR_POOL"] = answer or DEFAULT_CURSOR_POOL
        else:
            updates["CURSOR_POOL"] = DEFAULT_CURSOR_POOL
    return updates


# --- Cursor side ------------------------------------------------------------


def pool_entries(payload: Any) -> list[dict[str, Any]]:
    """Pool rows from ``GET /v0/private-workers/pools`` in any of its shapes.

    A bare string becomes ``{"poolName": <string>}``.
    """
    entries: Any = payload
    if isinstance(payload, dict):
        for key in ("pools", "items", "data"):
            if isinstance(payload.get(key), list):
                entries = payload[key]
                break
        else:
            entries = []
    rows: list[dict[str, Any]] = []
    for entry in entries or []:
        if isinstance(entry, str):
            rows.append({"poolName": entry})
        elif isinstance(entry, dict):
            rows.append(entry)
    return rows


def pool_name_of(entry: Mapping[str, Any]) -> str | None:
    for key in ("poolName", "name", "pool"):
        if isinstance(entry.get(key), str):
            return entry[key]
    return None


def pool_repo_of(entry: Mapping[str, Any]) -> str | None:
    """``owner/name`` of a repo-bound row, lower case; None for an any-repo row."""
    owner, name = entry.get("repoOwner"), entry.get("repoName")
    if isinstance(owner, str) and isinstance(name, str) and owner and name:
        return f"{owner}/{name}".lower()
    url = entry.get("repoUrl")
    if isinstance(url, str) and url.strip():
        return _repo_key(url)
    return None


def _repo_key(url: str) -> str:
    segments = [s for s in urlsplit(url).path.split("/") if s]
    path = "/".join(segments[-2:]) if len(segments) >= 2 else "/".join(segments)
    if path.endswith(".git"):
        path = path[:-4]
    return path.lower()


def pool_names(payload: Any) -> set[str]:
    """Pool names from ``GET /v0/private-workers/pools`` in any of its shapes."""
    return {name for name in (pool_name_of(row) for row in pool_entries(payload)) if name}


def ensure_pool(
    api: CursorAPI,
    pool: str,
    *,
    ready_timeout: int,
    repo_url: str | None = None,
    any_repo: bool = False,
) -> str:
    """Return ``exists`` or ``registered``. Raises CursorAPIError on a bad key.

    With ``repo_url`` the pool must exist as a row bound to that repository.
    An any-repo row of the same name does not count: a worker with ``origin``
    set never joins it. With ``any_repo`` the pool must exist as the any-repo
    row, which is the row any-repo workers join. With neither, any row of that
    name counts.
    """
    wanted = _repo_key(repo_url) if repo_url else None
    for row in pool_entries(api.list_pools()):
        if pool_name_of(row) != pool:
            continue
        repo = pool_repo_of(row)
        if any_repo:
            if repo is None:
                return "exists"
            continue
        if wanted is None or repo == wanted:
            return "exists"
    try:
        api.register_pool(pool, worker_ready_timeout_seconds=ready_timeout, repo_url=repo_url)
    except CursorAPIError as exc:
        if getattr(exc, "status_code", None) == 409:
            return "exists"
        raise
    return "registered"


# --- Orchestration ----------------------------------------------------------


def wait_for_watching(
    read_status: Callable[[], dict[str, Any] | None],
    *,
    timeout_secs: float = WATCH_TIMEOUT_SECS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any] | None:
    deadline = clock() + timeout_secs
    last: dict[str, Any] | None = None
    while clock() < deadline:
        last = read_status()
        if last and last.get("state") == "watching":
            return last
        sleep(3)
    return last


def run_up(
    args: argparse.Namespace,
    *,
    env_path: Path = ENV_PATH,
    ensure_worker_image: Callable[[], str],
    ensure_orchestrator_image: Callable[..., str],
    ensure_orchestrator: Callable[..., Any],
    read_status: Callable[[Config], dict[str, Any] | None],
    api_factory: Callable[[str, str], CursorAPI] = CursorAPI,
    ask_secret: Callable[[str], str] = getpass.getpass,
    ask: Callable[[str], str] = input,
    sleep: Callable[[float], None] = time.sleep,
    out=sys.stdout,
) -> int:
    def say(text: str) -> None:
        print(text, file=out, flush=True)

    # 1. .env
    load_dotenv_if_available(env_path)
    updates = collect_missing(
        os.environ, interactive=not args.non_interactive, ask_secret=ask_secret, ask=ask
    )
    if args.computer_use:
        updates["WORKER_COMPUTER_USE"] = "true"
    if getattr(args, "repo_url", None) and getattr(args, "any_repo", False):
        raise ConfigError("--repo-url and --any-repo exclude each other")
    if getattr(args, "repo_url", None):
        updates["CURSOR_POOL_REPO_URL"] = args.repo_url.strip()
        if os.environ.get("CURSOR_POOL_MODE", "").strip().lower() == "any-repo":
            updates["CURSOR_POOL_MODE"] = "repo"
    if getattr(args, "any_repo", False):
        updates["CURSOR_POOL_MODE"] = "any-repo"
        if os.environ.get("CURSOR_POOL_REPO_URL", "").strip():
            updates["CURSOR_POOL_REPO_URL"] = ""
    if updates:
        update_env_file(env_path, updates)
        os.environ.update(updates)
        say(f"[1/6] Config: saved {', '.join(sorted(updates))} to {env_path}")
    else:
        say(f"[1/6] Config: {env_path} has every key")

    config = Config.from_env(require_image=False)

    # 2. Cursor key + pool
    api = api_factory(config.cursor_api_key, config.cursor_api_url)
    try:
        outcome = ensure_pool(
            api,
            config.cursor_pool,
            ready_timeout=args.ready_timeout,
            repo_url=config.cursor_pool_repo_url,
            any_repo=config.cursor_pool_any_repo,
        )
    except CursorAPIError as exc:
        if getattr(exc, "status_code", None) in (401, 403):
            say(f"Cursor rejected the key: {config.redacted(exc)}")
            say("Use an Enterprise service-account key (Dashboard > Settings > API Keys > Service Accounts).")
        else:
            say(f"Cursor API did not answer: {config.redacted(exc)}")
            say("This is on Cursor's side. Run the command again in a minute.")
        return 1
    finally:
        api.close()
    verb = {"exists": "already registered", "registered": "registered now"}.get(outcome, outcome)
    if config.cursor_pool_repo_url:
        say(f"[2/6] Cursor pool {config.cursor_pool!r}: {verb}, bound to {config.cursor_pool_repo_url}")
    elif config.cursor_pool_any_repo:
        say(f"[2/6] Cursor pool {config.cursor_pool!r}: {verb} for any repository; workers clone on claim")
        say(f"{STEP_INDENT}At cursor.com/agents pick the pool under \"Any repo\".")
    else:
        say(f"[2/6] Cursor pool {config.cursor_pool!r}: {verb}; serves every repository the Cursor GitHub App can reach")
        say(f"{STEP_INDENT}At cursor.com/agents pick the repository, then the pool. Do not pick \"Any repo\".")

    # 3. worker image
    say("[3/6] Worker image: the sandbox image each Cursor agent runs in")
    image_name = ensure_worker_image()
    if os.environ.get("IMAGE_NAME", "").strip() != image_name:
        update_env_file(env_path, {"IMAGE_NAME": image_name})
        os.environ["IMAGE_NAME"] = image_name
        say(f"{STEP_INDENT}Pinned as IMAGE_NAME in {env_path}")

    # 4. orchestrator image
    say("[4/6] Orchestrator image: the sandbox image for the pool controller")
    ensure_orchestrator_image(rebuild=args.rebuild)

    # 5. orchestrator sandbox
    say("[5/6] Orchestrator sandbox: one long-lived sandbox that spawns a worker per task")
    config = Config.from_env()
    ensure_orchestrator(config, restart=getattr(args, "restart", False), recreate=args.rebuild)

    # 6. heartbeat
    if args.no_wait:
        say("[6/6] Heartbeat: skipped the wait (--no-wait)")
    else:
        say(f"[6/6] Heartbeat: waiting for the controller to report 'watching' (up to {int(WATCH_TIMEOUT_SECS)}s)...")
        status = wait_for_watching(
            lambda: read_status(config), timeout_secs=WATCH_TIMEOUT_SECS, sleep=sleep
        )
        state = (status or {}).get("state")
        if state == "watching":
            say(f"{STEP_INDENT}Controller is watching pool {config.cursor_pool!r}. Ready for tasks.")
        else:
            say(f"{STEP_INDENT}Not ready yet (state={state!r}). Check: cursor-tl-orchestrator-sandbox --logs")
            return 1

    say("")
    repo_hint = config.cursor_pool_repo_url or "https://github.com/org/repo"
    say("Send a task to the pool:")
    if config.cursor_pool_any_repo:
        say(f"  - cursor.com/agents: pick \"Any repo\", then Remote Machines, then the pool {config.cursor_pool!r}")
    else:
        say(f"  - cursor.com/agents: pick the repository, then Remote Machines, then the pool {config.cursor_pool!r}")
    say(f"  - Slack or GitHub: @Cursor pool={config.cursor_pool}")
    if config.cursor_pool_any_repo:
        say(f'  - CLI: cursor-tl-pool agent "Clone {repo_hint} and describe the change"')
    else:
        say(f'  - CLI: cursor-tl-pool agent "Describe the change" --repo {repo_hint}')
    say("")
    say("Watch it run:")
    say("  cursor-tl-orchestrator-sandbox --status   controller heartbeat")
    say("  cursor-tl-orchestrator-sandbox --logs     orchestrator log")
    say("  tl sbx ls                                 sandboxes: the orchestrator plus one worker per task")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cursor-tl-up", description=__doc__.split("\n\n")[0])
    parser.add_argument("--non-interactive", action="store_true", help="fail instead of asking for missing keys")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="rebuild the orchestrator image and recreate the orchestrator sandbox from it (after code changes)",
    )
    parser.add_argument("--computer-use", action="store_true", help="set WORKER_COMPUTER_USE=true: desktop image and --computer-use workers. Add --rebuild the first time, so the orchestrator image carries it")
    parser.add_argument("--ready-timeout", type=int, default=900, help="workerReadyTimeoutSeconds for a new pool")
    parser.add_argument(
        "--repo-url",
        help="pin the pool to one HTTPS repository; saved as CURSOR_POOL_REPO_URL",
    )
    parser.add_argument(
        "--any-repo",
        action="store_true",
        help="one pool row for every repository; workers clone on claim. Saved as CURSOR_POOL_MODE=any-repo",
    )
    parser.add_argument("--no-wait", action="store_true", help="do not wait for the heartbeat")
    parser.add_argument(
        "--restart",
        action="store_true",
        help="restart the orchestrator process even when its settings match .env",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    from .launch_orchestrator_sandbox import ensure_orchestrator, read_status
    from .orchestrator_image import ensure_orchestrator_image
    from .worker_image import ensure_worker_image

    try:
        return run_up(
            args,
            ensure_worker_image=lambda: ensure_worker_image(indent=STEP_INDENT),
            ensure_orchestrator_image=lambda rebuild: ensure_orchestrator_image(rebuild=rebuild, indent=STEP_INDENT),
            ensure_orchestrator=lambda config, **kw: ensure_orchestrator(config, indent=STEP_INDENT, **kw),
            read_status=read_status,
        )
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
