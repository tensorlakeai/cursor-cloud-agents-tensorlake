"""Environment contract for the Cursor-on-Tensorlake integration.

Two kinds of settings live here:

- ``Config``: operator settings read from ``.env`` (Cursor service-account key,
  pool name, Tensorlake image and sizing, janitor timings). The orchestrator,
  launcher, pool tool, and spawn hook all read it.
- ``Claim``: the per-request values the Cursor controller places in the spawn
  hook's environment (``CURSOR_AGENT_WORKER_ID``, ``CURSOR_REQUEST_ID``, repo
  URLs, ...). Only the spawn hook reads it.

``TENSORLAKE_API_KEY`` selects the Tensorlake project. ``.env`` is its only
source: ``load_dotenv_if_available`` lets ``.env`` win over the shell, and
``Config.tensorlake_kwargs`` passes the key to every SDK call. It never reaches
a worker sandbox. ``CURSOR_API_KEY`` does reach the worker process because the
Cursor CLI needs it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence, cast
from urllib.parse import urlsplit

LOGGER = logging.getLogger(__name__)

DEFAULT_CURSOR_API_URL = "https://api.cursor.com"
DEFAULT_CURSOR_POOL = "tensorlake"
DEFAULT_STATE_DIR = "~/.cursor-tensorlake"
DEFAULT_SANDBOX_CPUS = 2.0
DEFAULT_SANDBOX_MEMORY_MB = 4096
DEFAULT_SANDBOX_DISK_MB = 20480
DEFAULT_SANDBOX_TIMEOUT_SECS = 3600
DEFAULT_WORKER_IDLE_RELEASE_SECS = 300
DEFAULT_SESSION_RETENTION_SECS = 86400
DEFAULT_JANITOR_INTERVAL_SECS = 30.0
DEFAULT_WAKE_POLL_SECS = 15.0
DEFAULT_SANDBOX_LAUNCH_TIMEOUT_SECS = 10.0
DEFAULT_ORCHESTRATOR_CPUS = 1.0
DEFAULT_ORCHESTRATOR_MEMORY_MB = 2048
DEFAULT_ORCHESTRATOR_DISK_MB = 20480

# Paths inside the worker image. The image builder creates them.
SANDBOX_USER = "tl-user"
SANDBOX_HOME = "/home/tl-user"
WORKSPACE_DIR = f"{SANDBOX_HOME}/workspace"
# Multi-repository requests: the primary repository is WORKSPACE_DIR; every
# other repository gets its own root under here, one `--worker-dir` each.
EXTRA_REPOS_DIR = f"{SANDBOX_HOME}/repos"
# Extra roots with their zero-based request indexes, one tab-delimited entry per
# line. The checkout hook reads it so positional pairing survives filtering.
EXTRA_REPOS_MANIFEST = f"{EXTRA_REPOS_DIR}/.roots"
WORKER_LOG_DIR = "/var/log/cursor-tl"
WORKER_LOG_PATH = f"{WORKER_LOG_DIR}/worker.log"
CHECKOUT_LOG_PATH = f"{WORKER_LOG_DIR}/checkout.log"
CHECKOUT_HOOK_PATH = "/usr/local/bin/cursor-tl-checkout"
AGENT_BIN = "/usr/local/bin/agent"
LABELS_PATH = f"{WORKER_LOG_DIR}/labels.json"
SANDBOX_USER_SPEC = "1000:1000"
WORKER_PROCESS_NAME = "cursor-worker"

# Hosts a worker must reach. Used to validate SANDBOX_ALLOW_OUT.
REQUIRED_EGRESS = (
    "api2.cursor.sh",
    "api2direct.cursor.sh",
)


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class RepoSpec:
    url: str
    target_name: str


@dataclass(frozen=True)
class Config:
    cursor_api_key: str
    cursor_api_url: str
    cursor_pool: str
    cursor_pool_repo_url: str | None
    cursor_pool_any_repo: bool
    cursor_user_api_key: str | None
    image_name: str
    state_dir: Path
    sandbox_cpus: float
    sandbox_memory_mb: int
    sandbox_disk_mb: int
    sandbox_timeout_secs: int
    sandbox_allow_out: tuple[str, ...]
    worker_idle_release_secs: int
    worker_labels_json: str | None
    worker_computer_use: bool
    worker_share_desktop: str | None
    worker_display: str | None
    warm_idle: int
    max_workers: int
    session_retention_secs: int
    janitor_interval_secs: float
    wake_offline_claims: bool
    wake_poll_secs: float
    sandbox_launch_timeout_secs: float
    orchestrator_cpus: float
    orchestrator_memory_mb: int
    orchestrator_disk_mb: int
    organization_id: str | None
    project_id: str | None
    namespace: str | None
    repos: tuple[RepoSpec, ...]
    git_username: str | None
    git_token: str | None

    @classmethod
    def from_env(cls, *, require_image: bool = True, require_cursor_key: bool = True) -> "Config":
        """Read settings from the environment.

        ``require_cursor_key=False`` is for My Machines, which runs on a
        personal ``CURSOR_USER_API_KEY`` and needs no service account.
        """
        load_dotenv_if_available()
        env = os.environ

        required = {
            "TENSORLAKE_API_KEY": _clean(env.get("TENSORLAKE_API_KEY")),
        }
        if require_cursor_key:
            required["CURSOR_API_KEY"] = _clean(env.get("CURSOR_API_KEY"))
        if require_image:
            required["IMAGE_NAME"] = _clean(env.get("IMAGE_NAME"))
        missing = sorted(name for name, value in required.items() if value is None)
        if missing:
            raise ConfigError(
                "Missing required environment variables: " + ", ".join(missing)
            )

        state_dir = Path(_str(env, "STATE_DIR", DEFAULT_STATE_DIR)).expanduser()
        state_dir.mkdir(parents=True, exist_ok=True)

        pool_repo_url = _parse_pool_repo_url(env.get("CURSOR_POOL_REPO_URL"))
        pool_any_repo = _parse_pool_mode(env.get("CURSOR_POOL_MODE"))
        if pool_any_repo and pool_repo_url:
            raise ConfigError(
                "CURSOR_POOL_MODE=any-repo and CURSOR_POOL_REPO_URL exclude each other: "
                "an any-repo pool serves every repository, so unset one of them"
            )

        share_desktop = _parse_share_desktop(env.get("WORKER_SHARE_DESKTOP"))

        allow_out = _parse_list(env.get("SANDBOX_ALLOW_OUT"))
        if allow_out:
            for host in REQUIRED_EGRESS:
                if host not in allow_out:
                    raise ConfigError(
                        f"SANDBOX_ALLOW_OUT must include {host}; the worker cannot "
                        "reach Cursor without it"
                    )

        return cls(
            cursor_api_key=cast(str, required.get("CURSOR_API_KEY") or _clean(env.get("CURSOR_API_KEY")) or ""),
            cursor_api_url=_str(env, "CURSOR_API_URL", DEFAULT_CURSOR_API_URL).rstrip("/"),
            cursor_pool=_str(env, "CURSOR_POOL", DEFAULT_CURSOR_POOL),
            cursor_pool_repo_url=pool_repo_url,
            cursor_pool_any_repo=pool_any_repo,
            cursor_user_api_key=_clean(env.get("CURSOR_USER_API_KEY")),
            image_name=cast(str, required.get("IMAGE_NAME") or _clean(env.get("IMAGE_NAME")) or ""),
            state_dir=state_dir,
            sandbox_cpus=_positive_float(env, "SANDBOX_CPUS", DEFAULT_SANDBOX_CPUS),
            sandbox_memory_mb=_positive_int(env, "SANDBOX_MEMORY_MB", DEFAULT_SANDBOX_MEMORY_MB),
            sandbox_disk_mb=_positive_int(env, "SANDBOX_DISK_MB", DEFAULT_SANDBOX_DISK_MB),
            sandbox_timeout_secs=_non_negative_int(
                env, "SANDBOX_TIMEOUT_SECS", DEFAULT_SANDBOX_TIMEOUT_SECS
            ),
            sandbox_allow_out=allow_out,
            worker_idle_release_secs=_non_negative_int(
                env, "WORKER_IDLE_RELEASE_SECS", DEFAULT_WORKER_IDLE_RELEASE_SECS
            ),
            worker_labels_json=_parse_labels(env.get("WORKER_LABELS_JSON")),
            worker_computer_use=_bool(env.get("WORKER_COMPUTER_USE"), False),
            worker_share_desktop=share_desktop,
            worker_display=_parse_display(env.get("WORKER_DISPLAY"), share_desktop=share_desktop),
            warm_idle=_non_negative_int(env, "WARM_IDLE", 0),
            max_workers=_non_negative_int(env, "MAX_WORKERS", 0),
            session_retention_secs=_non_negative_int(
                env, "SESSION_RETENTION_SECS", DEFAULT_SESSION_RETENTION_SECS
            ),
            janitor_interval_secs=_positive_float(
                env, "JANITOR_INTERVAL_SECS", DEFAULT_JANITOR_INTERVAL_SECS
            ),
            wake_offline_claims=_bool(env.get("WAKE_OFFLINE_CLAIMS"), False),
            wake_poll_secs=_positive_float(env, "WAKE_POLL_SECS", DEFAULT_WAKE_POLL_SECS),
            sandbox_launch_timeout_secs=_positive_float(
                env, "SANDBOX_LAUNCH_TIMEOUT_SECS", DEFAULT_SANDBOX_LAUNCH_TIMEOUT_SECS
            ),
            orchestrator_cpus=_positive_float(
                env, "ORCHESTRATOR_CPUS", DEFAULT_ORCHESTRATOR_CPUS
            ),
            orchestrator_memory_mb=_positive_int(
                env, "ORCHESTRATOR_MEMORY_MB", DEFAULT_ORCHESTRATOR_MEMORY_MB
            ),
            orchestrator_disk_mb=_positive_int(
                env, "ORCHESTRATOR_DISK_MB", DEFAULT_ORCHESTRATOR_DISK_MB
            ),
            organization_id=_clean(env.get("TENSORLAKE_ORGANIZATION_ID")),
            project_id=_clean(env.get("TENSORLAKE_PROJECT_ID")),
            namespace=_clean(env.get("TENSORLAKE_NAMESPACE")),
            repos=parse_repos(env.get("REPOS")),
            git_username=_clean(env.get("GIT_USERNAME")),
            git_token=_clean(env.get("GIT_TOKEN")),
        )

    def redacted(self, value: object) -> str:
        return redact(str(value), self.secrets_for_redaction())

    def secrets_for_redaction(self) -> tuple[str, ...]:
        values = [
            self.cursor_api_key,
            self.cursor_user_api_key,
            self.git_token,
            os.environ.get("TENSORLAKE_API_KEY"),
        ]
        return tuple(value for value in values if value)

    def tensorlake_kwargs(self) -> dict[str, str]:
        """Credentials for every Tensorlake SDK call.

        The key travels explicitly. The SDK otherwise freezes the value of
        ``TENSORLAKE_API_KEY`` at import time, which may be before ``.env`` was
        loaded and so may be a key from another project.
        """
        kwargs: dict[str, str] = {}
        api_key = _clean(os.environ.get("TENSORLAKE_API_KEY"))
        if api_key:
            kwargs["api_key"] = api_key
        if self.organization_id:
            kwargs["organization_id"] = self.organization_id
        if self.project_id:
            kwargs["project_id"] = self.project_id
        if self.namespace:
            kwargs["namespace"] = self.namespace
        return kwargs


@dataclass(frozen=True)
class Claim:
    """Values the Cursor controller passes to the spawn hook."""

    worker_id: str
    pool: str
    request_id: str | None
    repo_urls: tuple[str, ...]
    worker_name: str | None
    # CURSOR_WAKE=1: Cursor is reviving a hibernated worker. No claim was made,
    # so there is nothing to release on failure. CURSOR_WAKE_TIMEOUT_MS is the
    # reconnect window that remains.
    wake: bool = False
    wake_timeout_ms: int | None = None

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> "Claim":
        env = os.environ if environment is None else environment
        worker_id = _clean(env.get("CURSOR_AGENT_WORKER_ID"))
        pool = _clean(env.get("CURSOR_POOL"))
        missing = [
            name
            for name, value in (
                ("CURSOR_AGENT_WORKER_ID", worker_id),
                ("CURSOR_POOL", pool),
            )
            if value is None
        ]
        if missing:
            raise ConfigError(
                "Missing claim variables: "
                + ", ".join(missing)
                + ". Start this command through `agent worker controller --spawn`."
            )
        return cls(
            worker_id=cast(str, worker_id),
            pool=cast(str, pool),
            # Absent in warm mode: the controller pre-spawns unclaimed workers.
            request_id=_clean(env.get("CURSOR_REQUEST_ID")),
            repo_urls=_parse_repo_urls(
                _clean(env.get("CURSOR_REPO_URLS")), _clean(env.get("CURSOR_REPO_URL"))
            ),
            worker_name=_clean(env.get("CURSOR_WORKER_NAME")),
            wake=_clean(env.get("CURSOR_WAKE")) == "1",
            wake_timeout_ms=_int_or_none(env.get("CURSOR_WAKE_TIMEOUT_MS")),
        )

    def origin_urls(self) -> tuple[str, ...]:
        """Every repository of the request as a credential-free HTTPS URL.

        The first entry is the primary repository. A bad primary URL fails the
        claim. A bad URL after it is dropped with a warning, because the
        primary repository is still worth serving.

        Cursor sends URLs without a scheme; duplicates are dropped, order is
        kept. ``repo`` and ``repo.git`` are the same repository.
        """
        return tuple(url for _, url in self._origin_entries())

    def _origin_entries(self) -> tuple[tuple[int, str], ...]:
        """Validated, unique origins paired with their request positions."""
        entries: list[tuple[int, str]] = []
        seen: set[str] = set()
        for position, raw in enumerate(self.repo_urls):
            try:
                url = _https_origin(raw)
            except ConfigError:
                if position == 0:
                    raise
                # The URL stays out of the log: a rejected one can hold
                # credentials, which is one of the reasons to reject it.
                LOGGER.warning(
                    "claim repository %d is not a credential-free HTTPS URL; ignored",
                    position + 1,
                )
                continue
            key = _repo_identity(url)
            if key not in seen:
                seen.add(key)
                entries.append((position, url))
        return tuple(entries)

    def primary_origin_url(self) -> str | None:
        """HTTPS origin the primary workspace advertises to Cursor.

        Cursor routes a repository request only to a worker whose workspace has
        that repository as ``origin``.
        """
        urls = self.origin_urls()
        return urls[0] if urls else None

    def extra_worker_dirs(self) -> tuple[tuple[str, str], ...]:
        """``(directory, origin)`` for each repository after the primary.

        Each one becomes a further ``--worker-dir`` under ``EXTRA_REPOS_DIR``,
        named after the repository. A name already used by the primary or by
        an earlier extra becomes ``<owner>-<name>``.
        """
        return tuple(
            (directory, url) for _, directory, url in self.extra_worker_roots()
        )

    def extra_worker_roots(self) -> tuple[tuple[int, str, str], ...]:
        """``(request index, directory, origin)`` for non-primary roots."""
        entries = self._origin_entries()
        roots: list[tuple[int, str, str]] = []
        taken: set[str] = {_owner_and_name(entries[0][1])[1]} if entries else set()
        for request_index, url in entries[1:]:
            owner, name = _owner_and_name(url)
            base = name if name not in taken else f"{owner}-{name}"
            candidate, n = base, 2
            while candidate in taken:
                candidate, n = f"{base}-{n}", n + 1
            taken.add(candidate)
            directory = f"{EXTRA_REPOS_DIR}/{candidate}"
            roots.append((request_index, directory, url))
        return tuple(roots)


def _https_origin(raw: str) -> str:
    url = raw.strip()
    if "://" not in url:
        url = f"https://{url}"
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ConfigError("CURSOR_REPO_URL must be a credential-free HTTPS repository URL")
    return url


def _repo_identity(url: str) -> str:
    """Key that makes two spellings of one repository equal.

    ``norm()`` in the checkout hook does the same, so a URL that the hook
    matches to a worker root does not become a second root here.
    """
    parts = urlsplit(url)
    path = parts.path.rstrip("/").lower()
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return f"{parts.hostname or ''}{path}".lower()


def _owner_and_name(url: str) -> tuple[str, str]:
    segments = [s for s in urlsplit(url).path.split("/") if s]
    name = segments[-1] if segments else "repo"
    if name.endswith(".git"):
        name = name[:-4]
    owner = segments[-2] if len(segments) >= 2 else "repo"

    def clean(part: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "-", part).strip("-.") or "repo"

    return clean(owner), clean(name)


def worker_command(
    config: Config,
    claim: Claim,
    *,
    has_repo: bool,
    extra_dirs: Sequence[str] = (),
    clone_repos: bool = False,
) -> list[str]:
    """The ``agent worker`` argv. Flags go on ``worker``; ``start`` takes only ``--verbose``.

    The worker id is not a flag. The CLI reads ``CURSOR_AGENT_WORKER_ID`` from
    the environment (see ``worker_environment``). ``--worker-dir`` repeats once
    per repository root; the first is the primary repository.

    ``clone_repos`` is the any-repo pool worker: no ``origin`` in the workspace,
    so no ``repo=`` label, and ``--clone-git-repos`` so Cursor clones the
    request's repositories after the claim. It implies ``--mint-github-token``
    and replaces the checkout hook.
    """
    command = [
        AGENT_BIN,
        "worker",
        "--pool",
        claim.pool,
        "--worker-dir",
        WORKSPACE_DIR,
    ]
    for directory in extra_dirs:
        command.extend(["--worker-dir", directory])
    command += [
        "--management-addr",
        "127.0.0.1:8080",
        "--idle-release-timeout",
        str(config.worker_idle_release_secs),
    ]
    if claim.worker_name:
        command.extend(["--name", claim.worker_name])
    if clone_repos:
        command.append("--clone-git-repos")
    elif has_repo:
        command.extend(["--mint-github-token", "--on-session-start", CHECKOUT_HOOK_PATH])
    if config.worker_labels_json:
        command.extend(["--labels-file", LABELS_PATH])
    command.extend(computer_use_flags(config))
    command.extend(["start", "--verbose"])
    return command


def desktop_environment(config: Config) -> dict[str, str]:
    """``DISPLAY`` for a computer-use worker, so the agent's own shells find it.

    Cursor's executor gets the display from ``--display``, but a
    ``google-chrome`` the agent starts from a terminal tool call reads the
    environment. Without this it fails to open, on a machine that has a
    perfectly good desktop.
    """
    if not (config.worker_computer_use and config.worker_display):
        return {}
    return {
        "DISPLAY": config.worker_display,
        "XAUTHORITY": f"{SANDBOX_HOME}/.Xauthority",
    }


def heartbeat_settings(config: Config) -> dict[str, object]:
    """Settings the orchestrator echoes and the launcher compares with ``.env``.

    Every one of them changes how the spawn hook launches a worker, so a change
    has to restart the orchestrator process instead of waiting for a worker to
    read it. The orchestrator writes these; ``cursor-tl-up`` reads them back.
    """
    return {
        "pool_mode": "any-repo" if config.cursor_pool_any_repo else "repo",
        "pool_repo_url": config.cursor_pool_repo_url,
        "computer_use": config.worker_computer_use,
        "display": config.worker_display,
        "share_desktop": config.worker_share_desktop,
    }


def computer_use_flags(config: Config) -> list[str]:
    """``worker`` flags for computer use; shared by pool workers and My Machines.

    Linux: ``--display`` pins the worker to an existing X display. The desktop
    worker image already boots a TigerVNC + Xfce session on ``:1``, so the
    default attaches to that one instead of letting the worker start a second
    desktop. ``WORKER_DISPLAY=managed`` restores the worker-managed desktop.
    """
    if not config.worker_computer_use:
        return []
    flags = ["--computer-use"]
    if config.worker_display:
        flags.extend(["--display", config.worker_display])
    if config.worker_share_desktop:
        flags.extend(["--share-desktop", config.worker_share_desktop])
    return flags


def worker_environment(config: Config, claim: Claim) -> dict[str, str]:
    """Allowlisted env for the worker process.

    ``CURSOR_API_URL`` and ``CURSOR_API_ENDPOINT`` stay out: they point at the
    REST host, and the worker's auth exchange runs on a different host. The
    request id, pool, and repo list stay out because the worker has no use for
    them. ``TENSORLAKE_*`` never enters a worker sandbox.
    """
    env = {
        "CURSOR_API_KEY": config.cursor_api_key,
        "CURSOR_AGENT_WORKER_ID": claim.worker_id,
        "HOME": SANDBOX_HOME,
        "USER": SANDBOX_USER,
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "GIT_TERMINAL_PROMPT": "0",
        "NODE_COMPILE_CACHE": "/tmp/cursor-compile-cache",
    }
    if claim.worker_name:
        env["CURSOR_WORKER_NAME"] = claim.worker_name
    env.update(desktop_environment(config))
    for name in ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
        value = os.environ.get(name, "").strip()
        if value:
            env[name] = value
    return env


SHARE_DESKTOP_MODES = ("view", "view_and_control")

# The desktop worker image boots TigerVNC + Xfce here before any worker starts.
DEFAULT_WORKER_DISPLAY = ":1"
MANAGED_DISPLAY_WORDS = ("managed", "auto", "none")


def _parse_display(value: str | None, *, share_desktop: str | None = None) -> str | None:
    """The X display a computer-use worker attaches to, or None for a managed one.

    Reusing the display the image already runs keeps one desktop per sandbox at
    a number the operator can predict, which is what makes an outside recorder
    able to film what the agent drives. ``WORKER_SHARE_DESKTOP`` documents the
    opposite: it shares a worker-created, isolated desktop, so it turns the
    default off unless ``WORKER_DISPLAY`` names a display explicitly.
    """
    display = _clean(value)
    if display is None:
        return None if share_desktop else DEFAULT_WORKER_DISPLAY
    if display.lower() in MANAGED_DISPLAY_WORDS:
        return None
    if not re.fullmatch(r":\d+(\.\d+)?", display):
        raise ConfigError(
            "WORKER_DISPLAY must be an X display such as :1, or 'managed' to let "
            "the worker start its own desktop"
        )
    return display


def _parse_share_desktop(value: str | None) -> str | None:
    mode = _clean(value)
    if mode is None or mode.lower() in ("false", "0", "no", "off"):
        return None
    if mode.lower() in ("true", "1", "yes", "on"):
        return "view_and_control"
    if mode not in SHARE_DESKTOP_MODES:
        raise ConfigError(
            "WORKER_SHARE_DESKTOP must be one of " + ", ".join(SHARE_DESKTOP_MODES)
        )
    return mode


_DOTENV_REPLACED: set[str] = set()


def load_dotenv_if_available(path: str | Path = ".env") -> None:
    """Load ``.env`` from the current directory. Its values win over the shell.

    The Tensorlake key selects the project. A shell that exports a different
    ``TENSORLAKE_API_KEY`` than ``.env``, for example an IDE terminal, would
    otherwise launch a second orchestrator in a second project for the same
    Cursor pool, and new chats would land in either one. So a key present in
    ``.env`` defines the value: non-blank replaces the shell value, blank
    removes it. Keys absent from ``.env`` keep their shell value.
    """
    try:
        from dotenv import dotenv_values
    except ImportError:
        return
    env_file = Path(path)
    if not env_file.is_file():
        return
    replaced: list[str] = []
    for key, value in dotenv_values(env_file).items():
        value = value or ""
        current = os.environ.get(key)
        if value.strip():
            if current is not None and current.strip() != value.strip():
                replaced.append(key)
            os.environ[key] = value
        elif current is not None:
            if current.strip():
                replaced.append(key)
            del os.environ[key]
    new = sorted(set(replaced) - _DOTENV_REPLACED)
    if new:
        _DOTENV_REPLACED.update(new)
        print(f"{env_file} overrides the shell for: {', '.join(new)}", file=sys.stderr)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def parse_repos(value: str | None) -> tuple[RepoSpec, ...]:
    if not value:
        return ()
    repos: list[RepoSpec] = []
    used: set[str] = set()
    for raw in value.split(","):
        url = raw.strip()
        if not url:
            continue
        name = base = repo_target_name(url)
        suffix = 2
        while name in used:
            name = f"{base}-{suffix}"
            suffix += 1
        used.add(name)
        repos.append(RepoSpec(url=url, target_name=name))
    return tuple(repos)


def repo_target_name(url: str) -> str:
    without_query = url.split("?", 1)[0].rstrip("/")
    tail = without_query.rsplit("/", 1)[-1]
    if ":" in tail and not tail.endswith(".git"):
        tail = tail.rsplit(":", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", tail).strip("-._")
    return cleaned or "repo"


def redact(text: str, secrets: Iterable[str]) -> str:
    redacted = text
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        redacted = redacted.replace(secret, "<redacted>")
    redacted = re.sub(r"(https?://)([^/\s:@]+):([^@\s/]+)@", r"\1<redacted>@", redacted)
    return redacted


def _parse_repo_urls(value: str | None, fallback: str | None) -> tuple[str, ...]:
    if value is None:
        return (fallback,) if fallback else ()
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ConfigError("CURSOR_REPO_URLS must be a JSON array of strings") from exc
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise ConfigError("CURSOR_REPO_URLS must be a JSON array of strings")
    return tuple(item.strip() for item in parsed if item.strip())


def _parse_pool_repo_url(value: str | None) -> str | None:
    """The repository the pool is bound to, as an HTTPS URL without ``.git``.

    Cursor keys a pool row by name plus repository. With ``--repository`` the
    controller registers the repo-bound row and claims only that repository's
    requests. Without it the controller also registers an any-repo row, and a
    worker whose workspace has ``origin`` set never joins that row.
    """
    value = _clean(value)
    if value is None:
        return None
    parts = urlsplit(value)
    segments = [s for s in parts.path.split("/") if s]
    if parts.scheme != "https" or not parts.hostname or len(segments) < 2:
        raise ConfigError(
            "CURSOR_POOL_REPO_URL must be an HTTPS repository URL such as "
            "https://github.com/owner/repo"
        )
    path = "/".join(segments)
    if path.endswith(".git"):
        path = path[:-4]
    return f"https://{parts.hostname}/{path}"


POOL_MODES = ("repo", "any-repo")


def _parse_pool_mode(value: str | None) -> bool:
    """True for ``CURSOR_POOL_MODE=any-repo``.

    ``repo`` (the default) seeds each worker workspace with the request's
    repository as ``origin``, so the worker joins the repo-bound pool row and
    the checkout hook fetches the code. ``any-repo`` starts workers with no
    ``origin`` and ``--clone-git-repos``: they join the any-repo row, one pool
    row serves every repository the GitHub App can reach, and Cursor clones
    after the claim.
    """
    mode = (_clean(value) or POOL_MODES[0]).lower()
    if mode not in POOL_MODES:
        raise ConfigError("CURSOR_POOL_MODE must be one of: " + ", ".join(POOL_MODES))
    return mode == "any-repo"


def _parse_labels(value: str | None) -> str | None:
    value = _clean(value)
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigError("WORKER_LABELS_JSON must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise ConfigError("WORKER_LABELS_JSON must be a JSON object")
    return json.dumps(parsed, separators=(",", ":"), sort_keys=True)


def _parse_list(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _int_or_none(value: str | None) -> int | None:
    value = _clean(value)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _str(env: Mapping[str, str], name: str, default: str) -> str:
    return _clean(env.get(name)) or default


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    value = _clean(env.get(name))
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if parsed < 1:
        raise ConfigError(f"{name} must be at least 1")
    return parsed


def _non_negative_int(env: Mapping[str, str], name: str, default: int) -> int:
    value = _clean(env.get(name))
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if parsed < 0:
        raise ConfigError(f"{name} must be zero or greater")
    return parsed


def _positive_float(env: Mapping[str, str], name: str, default: float) -> float:
    value = _clean(env.get(name))
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if parsed <= 0:
        raise ConfigError(f"{name} must be greater than 0")
    return parsed


def _bool(value: str | None, default: bool) -> bool:
    value = _clean(value)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}
