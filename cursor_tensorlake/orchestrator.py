"""Long-running control loop. Runs inside the orchestrator sandbox.

Three jobs, one process:

1. **Controller supervisor.** Runs Cursor's ``agent worker controller`` with
   ``--spawn cursor-tl-spawn`` and restarts it with backoff if it exits. The
   controller owns claiming; this process never claims.
2. **Janitor.** Every ``JANITOR_INTERVAL_SECS`` it walks this integration's
   worker sandboxes. A sandbox whose worker process has exited is suspended
   (hibernation: filesystem, memory, and checkout kept, no compute billed). A
   sandbox suspended longer than ``SESSION_RETENTION_SECS`` is terminated. The
   polling itself is SDK traffic, so Tensorlake's idle timer never fires on a
   sandbox with a live worker.
3. **Wake fallback.** Cursor advertises a follow-up for a hibernated worker as
   a claimed-but-offline pending request (``claimedWorkerId``). The docs do not
   promise the controller re-runs the spawn hook for those, so after a short
   grace period this loop runs the spawn logic itself. Spawn is idempotent, so
   a controller that does handle it costs one no-op.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable

from .config import (
    WORKER_PROCESS_NAME,
    Claim,
    Config,
    heartbeat_settings,
    load_dotenv_if_available,
)
from .cursor_api import CursorAPI, pending_entries
from .sandbox import (
    GONE_STATUSES,
    SLEEPING_STATUSES,
    is_not_found,
    process_status,
    sandbox_name_for,
    sandbox_status,
)
from .state import StateStore, WorkerRecord

LOGGER = logging.getLogger(__name__)

SPAWN_COMMAND = "cursor-tl-spawn"
# A running sandbox with no worker process for this long is a spawn that died
# half way. Suspend it rather than leave it billing.
ORPHAN_GRACE_SECONDS = 120.0
# Give the controller first shot at a claimed-offline request.
WAKE_GRACE_SECONDS = 20.0
WAKE_REPEAT_SECONDS = 90.0
CONTROLLER_BACKOFF_MIN = 5.0
CONTROLLER_BACKOFF_MAX = 60.0


def resolve_spawn_path(spawn: str = SPAWN_COMMAND) -> str:
    """The controller resolves ``--spawn`` relative to its cwd, so pass an absolute path."""
    if os.path.isabs(spawn):
        return spawn
    return shutil.which(spawn) or f"/usr/local/bin/{spawn}"


def controller_command(config: Config, spawn_path: str | None = None) -> list[str]:
    spawn_path = spawn_path or resolve_spawn_path()
    command = [
        "agent",
        "worker",
        "controller",
        "--spawn",
        spawn_path,
        "--api-key",
        config.cursor_api_key,
        "--pool",
        config.cursor_pool,
    ]
    if config.cursor_pool_repo_url:
        # Pins the controller to one repository: it registers the repo-bound
        # row and claims only that repository's requests. Without it the
        # controller claims every request for the pool name and also keeps an
        # any-repo row, which only a worker without ``origin`` joins
        # (CURSOR_POOL_MODE=any-repo).
        command.extend(["--repository", config.cursor_pool_repo_url])
    if config.warm_idle > 0:
        command.extend(["--warm-idle", str(config.warm_idle)])
    return command


class Orchestrator:
    def __init__(
        self,
        config: Config,
        api: CursorAPI,
        store: StateStore,
        *,
        list_sandboxes: Callable[[Config], list[Any]],
        connect: Callable[[Config, str], Any | None],
        spawn: Callable[[Config, Claim], Any],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.api = api
        self.store = store
        self._list_sandboxes = list_sandboxes
        self._connect = connect
        self._spawn = spawn
        self._clock = clock
        self.stop_event = threading.Event()
        self.controller_restarts = 0
        self.controller_pid: int | None = None
        self._first_seen_offline: dict[str, float] = {}
        self._last_wake: dict[str, float] = {}

    # ----- lifecycle -----

    def run(self) -> None:
        threads = [
            threading.Thread(target=self._controller_loop, name="controller", daemon=True),
            threading.Thread(target=self._janitor_loop, name="janitor", daemon=True),
        ]
        if self.config.wake_offline_claims:
            threads.append(threading.Thread(target=self._wake_loop, name="wake", daemon=True))
        for thread in threads:
            thread.start()
        LOGGER.info(
            "Orchestrator watching pool %r (warm_idle=%d, retention=%ds, idle_release=%ds)",
            self.config.cursor_pool,
            self.config.warm_idle,
            self.config.session_retention_secs,
            self.config.worker_idle_release_secs,
        )
        try:
            while not self.stop_event.wait(1.0):
                pass
        finally:
            self.stop_event.set()
            self._write_status("stopped")

    def stop(self) -> None:
        self.stop_event.set()

    # ----- controller -----

    def _controller_loop(self) -> None:
        backoff = CONTROLLER_BACKOFF_MIN
        while not self.stop_event.is_set():
            command = controller_command(self.config)
            LOGGER.info("Starting Cursor controller: %s", self.config.redacted(" ".join(command)))
            try:
                proc = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env={**os.environ, "CURSOR_API_KEY": self.config.cursor_api_key},
                )
            except OSError as exc:
                LOGGER.error("Cannot start controller: %s", exc)
                if self.stop_event.wait(backoff):
                    return
                backoff = min(backoff * 2, CONTROLLER_BACKOFF_MAX)
                continue
            self.controller_pid = proc.pid
            started = self._clock()
            assert proc.stdout is not None
            for line in proc.stdout:
                LOGGER.info("[controller] %s", self.config.redacted(line.rstrip()))
                if self.stop_event.is_set():
                    proc.terminate()
                    break
            code = proc.wait()
            self.controller_pid = None
            if self.stop_event.is_set():
                return
            self.controller_restarts += 1
            if self._clock() - started > 300:
                backoff = CONTROLLER_BACKOFF_MIN
            LOGGER.warning("Controller exited with code %s; restarting in %.0fs", code, backoff)
            if self.stop_event.wait(backoff):
                return
            backoff = min(backoff * 2, CONTROLLER_BACKOFF_MAX)

    # ----- janitor -----

    def _janitor_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.janitor_once()
            except Exception as exc:
                LOGGER.warning("Janitor pass failed: %s", self.config.redacted(exc))
            self.stop_event.wait(self.config.janitor_interval_secs)

    def janitor_once(self) -> dict[str, int]:
        """One reconciliation pass. Returns counts for the heartbeat and tests."""
        now = self._clock()
        counts = {"running": 0, "suspended": 0, "suspended_now": 0, "terminated_now": 0}
        seen: set[str] = set()
        for info in self._list_sandboxes(self.config):
            name = getattr(info, "name", "") or ""
            seen.add(name)
            status = sandbox_status(info)
            record = self.store.read(name) or WorkerRecord(
                sandbox_name=name, worker_id=name.removeprefix("cursor-"), pool=self.config.cursor_pool
            )
            record.sandbox_id = getattr(info, "sandbox_id", record.sandbox_id)

            if status in GONE_STATUSES:
                self.store.delete(name)
                continue

            if status in SLEEPING_STATUSES:
                counts["suspended"] += 1
                if record.suspended_at is None:
                    record.suspended_at = now
                    self.store.write(record)
                elif self.config.session_retention_secs and now - record.suspended_at > self.config.session_retention_secs:
                    self._terminate(name, record)
                    counts["terminated_now"] += 1
                continue

            if status != "running":
                continue  # pending / snapshotting / suspending: look again next pass

            counts["running"] += 1
            sandbox = self._connect(self.config, name)
            if sandbox is None:
                continue
            worker = self._worker_status(sandbox)
            if worker == "running":
                if record.suspended_at is not None:
                    record.suspended_at = None
                    self.store.write(record)
                continue
            if worker is None:
                started = record.last_started_at or record.created_at
                if now - started < ORPHAN_GRACE_SECONDS:
                    continue
                reason = "no worker process"
            else:
                reason = f"worker {worker}"
            self._suspend(sandbox, name, record, now, reason)
            counts["suspended_now"] += 1

        for name in set(self.store.all()) - seen:
            LOGGER.info("Dropping record for vanished sandbox %s", name)
            self.store.delete(name)

        self._write_status("watching", counts)
        return counts

    def _worker_status(self, sandbox: Any) -> str | None:
        try:
            return process_status(sandbox.get_process(WORKER_PROCESS_NAME)) or None
        except Exception as exc:
            if is_not_found(exc):
                return None
            LOGGER.debug("get_process failed: %s", exc.__class__.__name__)
            return "unknown"

    def _suspend(self, sandbox: Any, name: str, record: WorkerRecord, now: float, reason: str) -> None:
        if reason == "worker unknown":
            return
        try:
            sandbox.suspend()
        except Exception as exc:
            if is_not_found(exc):
                self.store.delete(name)
                return
            LOGGER.warning("Suspend of %s failed: %s", name, self.config.redacted(exc))
            return
        record.suspended_at = now
        self.store.write(record)
        LOGGER.info("Hibernated %s (%s)", name, reason)

    def _terminate(self, name: str, record: WorkerRecord) -> None:
        sandbox = self._connect(self.config, name)
        if sandbox is not None:
            try:
                sandbox.terminate()
            except Exception as exc:
                if not is_not_found(exc):
                    LOGGER.warning("Terminate of %s failed: %s", name, self.config.redacted(exc))
                    return
        self.store.delete(name)
        LOGGER.info("Terminated %s after %ds suspended", name, self.config.session_retention_secs)

    # ----- wake fallback -----

    def _wake_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.wake_once()
            except Exception as exc:
                LOGGER.warning("Wake pass failed: %s", self.config.redacted(exc))
            self.stop_event.wait(self.config.wake_poll_secs)

    def wake_once(self) -> list[str]:
        """Resume hibernated workers Cursor is waiting on. Returns woken worker ids."""
        now = self._clock()
        payload = self.api.list_pending_requests(pool=self.config.cursor_pool)
        entries = pending_entries(payload)
        active = {str(e.get("claimedWorkerId")) for e in entries if e.get("claimedWorkerId")}
        for stale in set(self._first_seen_offline) - active:
            self._first_seen_offline.pop(stale, None)
        woken: list[str] = []
        for entry in entries:
            worker_id = entry.get("claimedWorkerId")
            if not worker_id:
                continue
            worker_id = str(worker_id)
            first_seen = self._first_seen_offline.setdefault(worker_id, now)
            if now - first_seen < WAKE_GRACE_SECONDS:
                continue
            if now - self._last_wake.get(worker_id, 0.0) < WAKE_REPEAT_SECONDS:
                continue
            name = sandbox_name_for(worker_id)
            if self.store.read(name) is None and not self._owns(name):
                continue
            claim = Claim(
                worker_id=worker_id,
                pool=self.config.cursor_pool,
                request_id=str(entry.get("id") or "") or None,
                repo_urls=_entry_repo_urls(entry),
                worker_name=None,
            )
            LOGGER.info("Waking hibernated worker %s for request %s", worker_id, claim.request_id)
            self._last_wake[worker_id] = now
            try:
                self._spawn(self.config, claim)
                woken.append(worker_id)
            except Exception as exc:
                LOGGER.warning("Wake of %s failed: %s", worker_id, self.config.redacted(exc))
        return woken

    def _owns(self, name: str) -> bool:
        return any((getattr(info, "name", "") or "") == name for info in self._list_sandboxes(self.config))

    # ----- heartbeat -----

    def _write_status(self, state: str, counts: dict[str, int] | None = None) -> None:
        self.store.write_status(
            {
                "state": state,
                "pool": self.config.cursor_pool,
                # The launcher compares these with .env and restarts the
                # orchestrator when they differ.
                **heartbeat_settings(self.config),
                "controller_pid": self.controller_pid,
                "controller_restarts": self.controller_restarts,
                "sessions": counts or {},
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )


def _entry_repo_urls(entry: dict[str, Any]) -> tuple[str, ...]:
    urls: list[str] = []
    for key in ("repoUrl", "repositoryUrl", "repository"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            urls.append(value.strip())
            break
    repos = entry.get("repos") or entry.get("repositories")
    if isinstance(repos, list):
        for item in repos:
            url = item.get("url") if isinstance(item, dict) else item
            if isinstance(url, str) and url.strip() and url.strip() not in urls:
                urls.append(url.strip())
    return tuple(urls)


def _spawn_in_process(config: Config, claim: Claim) -> Any:
    from .sandbox import get_or_create_session_sandbox, list_session_sandboxes
    from .spawn import spawn_worker

    api = CursorAPI(config.cursor_api_key, config.cursor_api_url)
    try:
        return spawn_worker(
            config,
            claim,
            get_or_create=get_or_create_session_sandbox,
            release_claim=api.release_claim,
            store=StateStore(config.state_dir),
            list_sandboxes=list_session_sandboxes,
        )
    finally:
        api.close()


def main() -> int:
    load_dotenv_if_available()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
        stream=sys.stdout,
    )
    from .sandbox import connect_sandbox, list_session_sandboxes

    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = Config.from_env()
    if shutil.which("agent") is None:
        print("Error: the Cursor CLI (`agent`) is not on PATH in this environment.", file=sys.stderr)
        return 2
    api = CursorAPI(config.cursor_api_key, config.cursor_api_url)
    orchestrator = Orchestrator(
        config,
        api,
        StateStore(config.state_dir),
        list_sandboxes=list_session_sandboxes,
        connect=connect_sandbox,
        spawn=_spawn_in_process,
    )
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: orchestrator.stop())
    try:
        orchestrator.run()
    finally:
        api.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
