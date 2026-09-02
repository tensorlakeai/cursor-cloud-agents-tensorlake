"""The ``--spawn`` hook for ``agent worker controller``.

The controller runs this once per claimed request (and once per missing warm
worker in warm mode) with the claim in the environment. The hook binds the
worker id to one named Tensorlake sandbox and makes sure a Cursor worker with
that id is running inside it.

Idempotent per worker id:

- first claim: create the sandbox from the worker image, seed the workspace
  ``origin`` (one root per repository of the request), start the worker;
- worker already running (controller double-fire, or the wake fallback raced
  the controller): do nothing;
- sandbox suspended (hibernated after the worker idle-exited): resume it and
  start a worker with the same id. The checkout is still there.

``MAX_WORKERS`` caps the number of live worker sandboxes. When the cap is
reached the hook releases the claim without creating anything, so the request
waits in Cursor's queue until a worker suspends or ends. Claims for a worker
that already has a sandbox are never refused: a follow-up must always be able
to wake its own session.

On failure the hook releases the Cursor claim so the request re-queues, then
terminates a sandbox it created or suspends one that pre-existed. The Cursor
controller SIGKILLs the hook's process group after 60 seconds, so provisioning
runs in a detached child and the parent relays the exit status when it lands
inside the window.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from .config import (
    EXTRA_REPOS_DIR,
    EXTRA_REPOS_MANIFEST,
    LABELS_PATH,
    SANDBOX_HOME,
    SANDBOX_USER_SPEC,
    WORKER_LOG_PATH,
    WORKER_PROCESS_NAME,
    WORKSPACE_DIR,
    Claim,
    Config,
    ConfigError,
    load_dotenv_if_available,
    worker_command,
    worker_environment,
)
from .sandbox import (
    SLEEPING_STATUSES,
    bind_outcome,
    is_not_found,
    process_status,
    sandbox_name_for,
    sandbox_status,
)
from .state import StateStore, WorkerRecord

LOGGER = logging.getLogger(__name__)

_DETACHED_FLAG = "CURSOR_TL_SPAWN_DETACHED"
_DETACH_WAIT_SECONDS = 45
_PREP_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class SpawnResult:
    sandbox_id: str
    sandbox_name: str
    worker_id: str
    request_id: str | None
    bind_outcome: str
    action: str  # "started" or "already-running"


class SpawnError(RuntimeError):
    """Raised when the worker could not be started; the claim was released."""


class CapacityError(SpawnError):
    """Raised when MAX_WORKERS live sandboxes exist and this claim needs a new one."""


def active_worker_count(infos: Iterable[Any]) -> int:
    """Worker sandboxes that cost compute right now: anything not suspended."""
    return sum(1 for info in infos if sandbox_status(info) not in SLEEPING_STATUSES)


def check_capacity(config: Config, name: str, infos: Iterable[Any]) -> None:
    """Refuse a claim that would push live sandboxes past ``MAX_WORKERS``.

    A sandbox that already exists for this worker, in any state, passes. Only
    the creation of one more sandbox is gated.
    """
    if config.max_workers <= 0:
        return
    infos = list(infos)
    if any((getattr(info, "name", None) or "") == name for info in infos):
        return
    active = active_worker_count(infos)
    if active >= config.max_workers:
        raise CapacityError(
            f"MAX_WORKERS reached: {active} live worker sandboxes, limit {config.max_workers}; "
            "claim released so Cursor requeues the request"
        )


def _seed_repo_root(directory: str, origin: str | None) -> list[str]:
    """Steps that make ``directory`` a git repository with ``origin`` set."""
    d = shlex.quote(directory)
    steps = [
        f"mkdir -p {d}",
        f"if [ ! -d {d}/.git ]; then git -C {d} init -q; fi",
    ]
    if origin:
        url = shlex.quote(origin)
        steps.append(
            f"if git -C {d} remote get-url origin >/dev/null 2>&1; "
            f"then git -C {d} remote set-url origin {url}; "
            f"else git -C {d} remote add origin {url}; fi"
        )
    else:
        steps.append(
            f"if ! git -C {d} rev-parse --verify --quiet HEAD >/dev/null; then "
            f"git -C {d} -c user.email=worker@local -c user.name=cursor-worker "
            "commit -q --allow-empty -m workspace; fi"
        )
    return steps


def workspace_prep_script(
    origin: str | None,
    extras: Sequence[tuple[str, str]] = (),
    *,
    extra_request_indexes: Sequence[int] | None = None,
) -> str:
    """Shell that makes ``WORKSPACE_DIR`` a git repo with the right ``origin``.

    Cursor routes a repository request only to a worker whose workspace has that
    repository as ``origin``. With no repository (any-repo request without a
    repo, or a warm worker) an empty commit keeps the worker dir valid.

    ``extras`` are ``(directory, origin)`` pairs for the other repositories of a
    multi-repository request. ``extra_request_indexes`` preserves their original
    zero-based positions if invalid or duplicate claim URLs were filtered out.
    The checkout hook fetches each root after Cursor mints the token.
    """
    if extra_request_indexes is None:
        extra_request_indexes = range(1, len(extras) + 1)
    if len(extra_request_indexes) != len(extras):
        raise ValueError("extra_request_indexes must have one entry per extra root")
    steps = ["set -e", *_seed_repo_root(WORKSPACE_DIR, origin)]
    for directory, url in extras:
        steps.extend(_seed_repo_root(directory, url))
    # Record the request order of the extra roots; rewrite it whenever the claim
    # names its repositories, so a reused sandbox does not keep the list of an
    # earlier request. A claim with no URLs at all (a wake, or an any-repo
    # request) says nothing about the roots, so it leaves the list untouched.
    manifest = shlex.quote(EXTRA_REPOS_MANIFEST)
    mkdir = f"mkdir -p {shlex.quote(EXTRA_REPOS_DIR)}"
    if extras:
        listed = " ".join(
            f"{request_index} {shlex.quote(directory)}"
            for request_index, (directory, _) in zip(extra_request_indexes, extras)
        )
        steps.extend([mkdir, f"printf '%s\\t%s\\n' {listed} > {manifest}"])
    elif origin:
        steps.extend([mkdir, f": > {manifest}"])
    return " && ".join(steps)


def any_repo_prep_script() -> str:
    """Shell for an any-repo pool worker: existing directories, no ``origin``.

    A worker whose workspace has no git remote carries no ``repo=`` label and
    joins the any-repo row of the pool. Cursor then clones the request's
    repositories into the workspace after the claim (``--clone-git-repos``).
    A ``.git`` seeded here would give the worker a repository identity again,
    so the script only makes sure the directories exist.
    """
    return " && ".join(
        ["set -e", f"mkdir -p {shlex.quote(WORKSPACE_DIR)}", f"mkdir -p {shlex.quote(EXTRA_REPOS_DIR)}"]
    )


def worker_launch_script(
    config: Config,
    claim: Claim,
    *,
    has_repo: bool,
    extra_dirs: Sequence[str] = (),
    clone_repos: bool = False,
) -> str:
    command = shlex.join(
        worker_command(config, claim, has_repo=has_repo, extra_dirs=extra_dirs, clone_repos=clone_repos)
    )
    return f"exec {command} >> {shlex.quote(WORKER_LOG_PATH)} 2>&1"


def worker_running(sandbox: Any) -> bool:
    try:
        proc = sandbox.get_process(WORKER_PROCESS_NAME)
    except Exception as exc:  # not found, or sandbox not routable yet
        if is_not_found(exc):
            return False
        LOGGER.debug("get_process(%s) failed: %s", WORKER_PROCESS_NAME, exc.__class__.__name__)
        return False
    return process_status(proc) == "running"


def _clear_stale_worker(sandbox: Any) -> None:
    """Drop a finished managed process so its name can be reused."""
    try:
        proc = sandbox.get_process(WORKER_PROCESS_NAME)
    except Exception:
        return
    if process_status(proc) == "running":
        return
    try:
        sandbox.kill_process(WORKER_PROCESS_NAME)
    except Exception as exc:
        LOGGER.debug("kill_process(%s) on stale worker: %s", WORKER_PROCESS_NAME, exc.__class__.__name__)


def start_worker_process(
    config: Config,
    claim: Claim,
    sandbox: Any,
    *,
    has_repo: bool,
    extra_dirs: Sequence[str] = (),
    clone_repos: bool = False,
) -> Any:
    script = worker_launch_script(
        config, claim, has_repo=has_repo, extra_dirs=extra_dirs, clone_repos=clone_repos
    )
    kwargs: dict[str, Any] = dict(
        env=worker_environment(config, claim),
        working_dir=WORKSPACE_DIR,
        user=SANDBOX_USER_SPEC,
        name=WORKER_PROCESS_NAME,
        # Exit 0 is the idle release; exit non-zero is a real failure the janitor
        # should see. Neither should be restarted behind Cursor's back.
        restart={"policy": "never"},
    )
    try:
        return sandbox.start_process("bash", ["-c", script], **kwargs)
    except Exception as first:
        _clear_stale_worker(sandbox)
        try:
            return sandbox.start_process("bash", ["-c", script], **kwargs)
        except Exception:
            raise first


def spawn_worker(
    config: Config,
    claim: Claim,
    *,
    get_or_create: Callable[[Config, str], Any],
    release_claim: Callable[[str], None] | None,
    store: StateStore | None,
    list_sandboxes: Callable[[Config], Iterable[Any]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> SpawnResult:
    name = sandbox_name_for(claim.worker_id)
    sandbox: Any | None = None
    outcome = "unknown"
    try:
        if list_sandboxes is not None and config.max_workers > 0:
            check_capacity(config, name, list_sandboxes(config))
        bound_sandbox: Any = get_or_create(config, name)
        sandbox = bound_sandbox
        outcome = bind_outcome(bound_sandbox)
        LOGGER.info(
            "Sandbox %s (%s): %s%s",
            name,
            getattr(bound_sandbox, "sandbox_id", "?"),
            outcome,
            f" (wake, {claim.wake_timeout_ms} ms left)" if claim.wake else "",
        )
        if claim.wake and outcome == "created":
            LOGGER.warning(
                "Wake for %s found no preserved sandbox; provisioning fresh. "
                "The retention window may have passed.",
                claim.worker_id,
            )

        if worker_running(bound_sandbox):
            LOGGER.info("Worker %s already running in %s; nothing to do", claim.worker_id, name)
            _record(store, name, claim, bound_sandbox, outcome, started=False)
            return SpawnResult(
                sandbox_id=str(getattr(bound_sandbox, "sandbox_id", "")),
                sandbox_name=name,
                worker_id=claim.worker_id,
                request_id=claim.request_id,
                bind_outcome=outcome,
                action="already-running",
            )

        if config.cursor_pool_any_repo:
            # Any-repo worker: no origin, no extra roots. Cursor clones.
            origin = None
            extras: list[tuple[str, str]] = []
            prep_script = any_repo_prep_script()
        else:
            origin = claim.primary_origin_url()
            extra_roots = claim.extra_worker_roots()
            extras = [(directory, url) for _, directory, url in extra_roots]
            prep_script = workspace_prep_script(
                origin,
                extras,
                extra_request_indexes=[request_index for request_index, _, _ in extra_roots],
            )
        prep = bound_sandbox.run(
            "bash",
            ["-c", prep_script],
            env={"HOME": SANDBOX_HOME, "PATH": "/usr/local/bin:/usr/bin:/bin"},
            user=SANDBOX_USER_SPEC,
            timeout=_PREP_TIMEOUT_SECONDS,
        )
        if getattr(prep, "exit_code", 1) != 0:
            raise SpawnError(
                "workspace preparation failed: "
                + (getattr(prep, "stderr", "") or getattr(prep, "stdout", "") or "").strip()[:500]
            )

        if config.worker_labels_json:
            bound_sandbox.write_file(LABELS_PATH, config.worker_labels_json.encode("utf-8"))

        start_worker_process(
            config,
            claim,
            bound_sandbox,
            has_repo=origin is not None,
            extra_dirs=[directory for directory, _ in extras],
            clone_repos=config.cursor_pool_any_repo,
        )

        deadline = clock() + config.sandbox_launch_timeout_secs
        while True:
            proc = bound_sandbox.get_process(WORKER_PROCESS_NAME)
            status = process_status(proc)
            if status != "running":
                raise SpawnError(
                    f"worker process ended during launch (status={status}, "
                    f"exit_code={getattr(proc, 'exit_code', None)}); see {WORKER_LOG_PATH} in {name}"
                )
            if clock() >= deadline:
                break
            sleep(1.0)

        _record(store, name, claim, bound_sandbox, outcome, started=True)
        return SpawnResult(
            sandbox_id=str(getattr(bound_sandbox, "sandbox_id", "")),
            sandbox_name=name,
            worker_id=claim.worker_id,
            request_id=claim.request_id,
            bind_outcome=outcome,
            action="started",
        )
    except Exception as exc:
        LOGGER.error("Spawn failed for worker %s: %s", claim.worker_id, config.redacted(exc))
        if release_claim is not None and claim.request_id and not claim.wake:
            try:
                release_claim(claim.request_id)
                LOGGER.info("Released Cursor claim %s", claim.request_id)
            except Exception as release_exc:
                LOGGER.warning("Could not release claim %s: %s", claim.request_id, config.redacted(release_exc))
        if sandbox is not None:
            _rollback(sandbox, created=(outcome == "created"))
        raise


def _rollback(sandbox: Any, *, created: bool) -> None:
    try:
        if created:
            sandbox.terminate()
            LOGGER.info("Terminated freshly created sandbox after failed spawn")
        elif sandbox_status(sandbox) in {"running", "pending"}:
            sandbox.suspend()
            LOGGER.info("Suspended pre-existing sandbox after failed spawn")
    except Exception as exc:
        LOGGER.warning("Rollback failed: %s", exc.__class__.__name__)


def _record(
    store: StateStore | None,
    name: str,
    claim: Claim,
    sandbox: Any,
    outcome: str,
    *,
    started: bool,
) -> None:
    if store is None:
        return
    record = store.read(name) or WorkerRecord(
        sandbox_name=name, worker_id=claim.worker_id, pool=claim.pool
    )
    record.sandbox_id = str(getattr(sandbox, "sandbox_id", "") or record.sandbox_id or "")
    record.pool = claim.pool
    if claim.request_id:
        record.request_id = claim.request_id
    try:
        origin = claim.primary_origin_url()
    except ConfigError:
        origin = None
    if origin:
        record.repo_url = origin
    record.bind_outcome = outcome
    if started:
        record.last_started_at = time.time()
    record.suspended_at = None
    store.write(record)


def run_spawn(argv_env: dict[str, str] | None = None) -> int:
    """Provision synchronously. Called by ``main`` in the detached child."""
    from .cursor_api import CursorAPI
    from .sandbox import get_or_create_session_sandbox, list_session_sandboxes

    config = Config.from_env()
    claim = Claim.from_env(argv_env)
    api = CursorAPI(config.cursor_api_key, config.cursor_api_url)
    store = StateStore(config.state_dir)
    try:
        result = spawn_worker(
            config,
            claim,
            get_or_create=get_or_create_session_sandbox,
            release_claim=api.release_claim,
            store=store,
            list_sandboxes=list_session_sandboxes,
        )
    except Exception as exc:
        print(f"cursor-tl-spawn: failed to start worker: {config.redacted(exc)}", file=sys.stderr)
        return 1
    finally:
        api.close()
    print(json.dumps(result.__dict__, separators=(",", ":")))
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s cursor-tl-spawn %(message)s",
        stream=sys.stderr,
    )
    load_dotenv_if_available()

    if os.environ.get(_DETACHED_FLAG) != "1" and os.environ.get("CURSOR_TL_SPAWN_NO_DETACH") != "1":
        child = subprocess.Popen(
            [sys.executable, "-m", "cursor_tensorlake.spawn"],
            env={**os.environ, _DETACHED_FLAG: "1"},
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        try:
            return child.wait(timeout=_DETACH_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            print(f"cursor-tl-spawn: still provisioning as PID {child.pid}", file=sys.stderr)
            return 0

    try:
        return run_spawn()
    except ConfigError as exc:
        print(f"cursor-tl-spawn: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
