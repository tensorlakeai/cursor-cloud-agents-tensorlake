"""Shared pytest configuration.

``Config.from_env`` loads ``.env`` from the current directory, so a developer's
real ``.env`` in the repo root would leak credentials into tests. Run every test
from a fresh temp directory so the suite behaves the same with or without one.
"""

from __future__ import annotations

import tempfile

import pytest


@pytest.fixture(autouse=True)
def _isolate_cwd_from_repo_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tempfile.mkdtemp(prefix="cursor-tl-tests-"))
    for name in list(__import__("os").environ):
        if name.startswith(("CURSOR_", "TENSORLAKE_", "IMAGE_", "SANDBOX_", "WORKER_", "WARM_", "SESSION_", "JANITOR_", "WAKE_", "REPOS", "GIT_", "STATE_DIR", "ORCHESTRATOR_")):
            monkeypatch.delenv(name, raising=False)
