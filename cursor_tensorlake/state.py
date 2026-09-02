"""Local index of worker sandboxes the orchestrator owns.

Tensorlake sandboxes have no labels, so the janitor keeps one JSON record per
worker sandbox under ``STATE_DIR/workers/<sandbox-name>.json``. The sandbox
name is derivable from the worker id, so a lost index degrades gracefully: the
janitor still finds sandboxes by name prefix; it only loses ``suspended_at``
and falls back to "suspended now".
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import atomic_write_text

LOGGER = logging.getLogger(__name__)


@dataclass
class WorkerRecord:
    sandbox_name: str
    worker_id: str
    pool: str
    sandbox_id: str | None = None
    request_id: str | None = None
    repo_url: str | None = None
    created_at: float = field(default_factory=time.time)
    last_started_at: float | None = None
    suspended_at: float | None = None
    bind_outcome: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "WorkerRecord":
        data: dict[str, Any] = json.loads(text)
        known = {name for name in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{key: value for key, value in data.items() if key in known})


class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)
        self.workers_dir = self.state_dir / "workers"
        self.status_path = self.state_dir / "status.json"
        self._lock = threading.Lock()

    def _path(self, sandbox_name: str) -> Path:
        return self.workers_dir / f"{sandbox_name}.json"

    def write(self, record: WorkerRecord) -> None:
        with self._lock:
            atomic_write_text(self._path(record.sandbox_name), record.to_json())

    def read(self, sandbox_name: str) -> WorkerRecord | None:
        try:
            return WorkerRecord.from_json(self._path(sandbox_name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError) as exc:
            LOGGER.warning("Unreadable worker record %s: %s", sandbox_name, exc)
            return None

    def delete(self, sandbox_name: str) -> None:
        with self._lock:
            try:
                self._path(sandbox_name).unlink()
            except FileNotFoundError:
                pass

    def all(self) -> dict[str, WorkerRecord]:
        records: dict[str, WorkerRecord] = {}
        if not self.workers_dir.exists():
            return records
        for path in sorted(self.workers_dir.glob("*.json")):
            record = self.read(path.stem)
            if record is not None:
                records[record.sandbox_name] = record
        return records

    def write_status(self, payload: dict[str, Any]) -> None:
        try:
            atomic_write_text(self.status_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
        except OSError as exc:
            LOGGER.debug("status.json write failed: %s", exc)


__all__ = ["StateStore", "WorkerRecord"]
