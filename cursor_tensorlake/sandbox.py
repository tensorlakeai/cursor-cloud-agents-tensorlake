"""Name, find, and bind Tensorlake sandboxes for Cursor workers.

Tensorlake sandboxes carry no labels, so the deterministic **name** is the
reconciliation key. ``cursor-<worker-id>`` maps one Cursor worker id to one
sandbox for its whole life: created on the first claim, suspended when the
worker idle-exits, resumed when Cursor wakes the same worker id, terminated by
the janitor after the retention window.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, Iterable

from tensorlake.sandbox import Sandbox, SandboxInfo, SandboxNotFoundError

from .config import Config

LOGGER = logging.getLogger(__name__)

SESSION_PREFIX = "cursor-"
ORCHESTRATOR_PREFIX = "cursor-tl-orch-"
MY_MACHINE_PREFIX = "cursor-tl-mm-"
SANDBOX_NAME_MAX_LENGTH = 63

# Statuses that mean the sandbox no longer exists as a reusable environment.
GONE_STATUSES = {"terminated", "timeout"}
SLEEPING_STATUSES = {"suspended", "suspending"}


def sandbox_name_for(worker_id: str) -> str:
    """``cursor-<worker-id>`` when the id is already a safe slug, else a hashed slug."""
    direct = f"{SESSION_PREFIX}{worker_id}"
    if len(direct) <= SANDBOX_NAME_MAX_LENGTH and re.fullmatch(
        r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", worker_id
    ):
        return direct
    digest = hashlib.sha256(worker_id.encode("utf-8")).hexdigest()[:12]
    slug = safe_identifier(worker_id)[:43].rstrip("-") or "worker"
    return f"{SESSION_PREFIX}{slug}-{digest}"


def orchestrator_sandbox_name(pool: str) -> str:
    digest = hashlib.sha256(pool.encode("utf-8")).hexdigest()[:8]
    return f"{ORCHESTRATOR_PREFIX}{digest}"


def my_machine_sandbox_name(machine_name: str) -> str:
    slug = safe_identifier(machine_name)[: SANDBOX_NAME_MAX_LENGTH - len(MY_MACHINE_PREFIX)]
    return f"{MY_MACHINE_PREFIX}{slug.rstrip('-') or 'machine'}"


def safe_identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")


def sandbox_status(sandbox_or_info: Any) -> str:
    status = getattr(sandbox_or_info, "status", "")
    value = getattr(status, "value", status)
    return str(value or "").lower()


def process_status(process: Any) -> str:
    status = getattr(process, "status", "")
    return str(getattr(status, "value", status) or "").lower()


def is_not_found(exc: BaseException) -> bool:
    if isinstance(exc, SandboxNotFoundError):
        return True
    if getattr(exc, "status_code", None) == 404:
        return True
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) == 404:
        return True
    return exc.__class__.__name__ in {"NotFoundError", "NotFoundException"}


def connect_sandbox(config: Config, name: str) -> Sandbox | None:
    """Attach to the sandbox that holds ``name``; ``None`` when none does. Does not resume."""
    try:
        return Sandbox.connect(name, **config.tensorlake_kwargs())
    except SandboxNotFoundError:
        return None


def get_or_create_session_sandbox(config: Config, name: str) -> Sandbox:
    """Bind ``name`` to one running sandbox: attach, resume, or create.

    Sizing, image, and network policy apply only when the call creates a new
    sandbox. The returned handle's ``bind_outcome`` says which path ran.
    """
    return Sandbox.get_or_create(
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


def list_session_sandboxes(config: Config) -> list[SandboxInfo]:
    """This integration's worker sandboxes, by name prefix, excluding gone ones."""
    return list(_filter_by_prefix(Sandbox.list(**config.tensorlake_kwargs()), SESSION_PREFIX))


def _filter_by_prefix(infos: Iterable[Any], prefix: str) -> Iterable[Any]:
    for info in infos:
        name = getattr(info, "name", None) or ""
        if not name.startswith(prefix):
            continue
        if name.startswith(ORCHESTRATOR_PREFIX) or name.startswith(MY_MACHINE_PREFIX):
            continue
        if sandbox_status(info) in GONE_STATUSES:
            continue
        yield info


def bind_outcome(sandbox: Any) -> str:
    return str(getattr(sandbox, "bind_outcome", "") or "attached")


__all__ = [
    "GONE_STATUSES",
    "SESSION_PREFIX",
    "SLEEPING_STATUSES",
    "bind_outcome",
    "connect_sandbox",
    "get_or_create_session_sandbox",
    "is_not_found",
    "list_session_sandboxes",
    "my_machine_sandbox_name",
    "orchestrator_sandbox_name",
    "process_status",
    "safe_identifier",
    "sandbox_name_for",
    "sandbox_status",
]
