"""Build the Tensorlake image each Cursor worker sandbox boots from.

The image name derives from a hash of the build recipe. Editing any step gives
a new name; re-running with an unchanged recipe reuses the registered image.
Set ``IMAGE_NAME`` to pin an explicit name.

The recipe on top of a Tensorlake Ubuntu base:

- ``ca-certificates``, ``curl``, ``git``, ``jq``, ``openssh-client``, ``python3``
  (the checkout hook parses JSON with python3);
- the Cursor CLI from the **lab** channel, pinned by version and BLAKE2 checksum,
  under ``/opt/cursor-agent`` with ``/usr/local/bin/agent`` linked to it, so any
  user can run it. The stable channel has no ``agent worker controller``;
- the GitHub CLI (best-effort convenience for agents);
- the checkout hook at ``/usr/local/bin/cursor-tl-checkout``;
- ``/home/tl-user/workspace`` and ``/var/log/cursor-tl`` owned by uid 1000;
- with ``WORKER_COMPUTER_USE=true``: the base switches to ``tensorlake/ubuntu-vnc``
  (Xfce, TigerVNC, Google Chrome, Firefox), the recipe adds the packages
  Cursor's Linux computer use needs (``xdotool``, ``ffmpeg``, X11 utilities),
  and it clears Chrome's first-run dialog so the browser opens on the page the
  agent asked for.

The orchestrator image reuses ``cli_recipe()`` so both images carry the same CLI.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from pathlib import Path

from tensorlake import Image, find_sandbox_image_by_name

from .config import (
    CHECKOUT_HOOK_PATH,
    EXTRA_REPOS_DIR,
    WORKER_LOG_DIR,
    WORKSPACE_DIR,
    load_dotenv_if_available,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_IMAGE = "tensorlake/ubuntu-minimal"
DEFAULT_DESKTOP_BASE_IMAGE = "tensorlake/ubuntu-vnc"
DEFAULT_IMAGE_PREFIX = "cursor-tl-worker"
DEFAULT_CLI_VERSION = "2026.09.02-e3e9343"
DEFAULT_CLI_B2SUM = (
    "a29694895b3b5d90e7d751eddfd2682e4872c9db6c2a3dee9eb3940a74d58c1c"
    "d498be3cc5ddf22bc7a22f2e8011e7430e5392149d1704ce952b368fa105d484"
)
DEFAULT_CLI_ARCH = "x64"
DEFAULT_CPU = 2.0
DEFAULT_MEMORY_MB = 4096
DEFAULT_DISK_MB = 20480

PACKAGE_DIR = Path(__file__).resolve().parent

APT_CORE = (
    "export DEBIAN_FRONTEND=noninteractive && apt-get update && "
    "apt-get install -y --no-install-recommends "
    "ca-certificates curl git jq openssh-client python3 && "
    "rm -rf /var/lib/apt/lists/*"
)

APT_GH = (
    "export DEBIAN_FRONTEND=noninteractive && "
    "( install -d -m 0755 /etc/apt/keyrings && "
    "curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg "
    "-o /etc/apt/keyrings/githubcli-archive-keyring.gpg && "
    "chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg && "
    'echo "deb [arch=$(dpkg --print-architecture) '
    'signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] '
    'https://cli.github.com/packages stable main" '
    "> /etc/apt/sources.list.d/github-cli.list && "
    "apt-get update && apt-get install -y --no-install-recommends gh && "
    "rm -rf /var/lib/apt/lists/* ) || echo 'gh install skipped'"
)

# Cursor's documented Linux dependencies for --computer-use. tigervnc, xfce4,
# Google Chrome, and Firefox already ship in tensorlake/ubuntu-vnc. Ubuntu's
# "chromium" apt package is only a snap stub, so it is deliberately not added.
APT_DESKTOP = (
    "export DEBIAN_FRONTEND=noninteractive && apt-get update && "
    "apt-get install -y --no-install-recommends "
    "dbus-x11 ffmpeg tigervnc-standalone-server x11-utils x11-xserver-utils "
    "xdotool xfce4 && "
    "rm -rf /var/lib/apt/lists/*"
)

# Google Chrome shows an "Additional Terms of Service" dialog on a profile's
# first run, which covers the whole screen and hides whatever the agent meant
# to look at. The sentinel file marks the default profile as already seen. The
# managed policy covers the prompts that do not depend on the profile, and
# applies however the browser is started, by the agent or by Cursor itself.
CHROME_FIRST_RUN = (
    "install -d -m 0755 -o 1000 -g 1000 /home/tl-user/.config "
    "/home/tl-user/.config/google-chrome && "
    "touch '/home/tl-user/.config/google-chrome/First Run' && "
    "chown 1000:1000 '/home/tl-user/.config/google-chrome/First Run' && "
    "install -d -m 0755 /etc/opt/chrome/policies/managed && "
    "printf '%s' '{\"DefaultBrowserSettingEnabled\": false, "
    '\"MetricsReportingEnabled\": false, \"PromotionalTabsEnabled\": false, '
    "\"BrowserSignin\": 0}' "
    "> /etc/opt/chrome/policies/managed/cursor-tl.json"
)

WORKER_DIRS = (
    f"install -d -m 0755 -o 1000 -g 1000 {WORKSPACE_DIR} {EXTRA_REPOS_DIR} {WORKER_LOG_DIR} && "
    "install -d -m 1777 /tmp/cursor-compile-cache"
)


def cli_install_command(version: str, b2sum: str, arch: str) -> str:
    url = f"https://downloads.cursor.com/lab/{version}/linux/{arch}/agent-cli-package.tar.gz"
    check = (
        f'echo "{b2sum}  /tmp/cursor-agent.tar.gz" | b2sum --check --status'
        if b2sum
        else "echo 'WARNING: CURSOR_CLI_B2SUM is empty; checksum skipped'"
    )
    return (
        "set -e && "
        f"curl -fsSL '{url}' -o /tmp/cursor-agent.tar.gz && "
        f"{check} && "
        "rm -rf /opt/cursor-agent && mkdir -p /opt/cursor-agent && "
        "tar -xzf /tmp/cursor-agent.tar.gz -C /opt/cursor-agent --strip-components=1 && "
        "rm -f /tmp/cursor-agent.tar.gz && "
        "chmod -R a+rX /opt/cursor-agent && "
        "ln -sf /opt/cursor-agent/cursor-agent /usr/local/bin/agent && "
        "ln -sf /opt/cursor-agent/cursor-agent /usr/local/bin/cursor-agent && "
        "/usr/local/bin/agent --version"
    )


def cli_recipe(version: str, b2sum: str, arch: str) -> list[tuple[str, object]]:
    """Steps shared by the worker and orchestrator images."""
    return [
        ("run", APT_CORE),
        ("run", cli_install_command(version, b2sum, arch)),
        ("run", APT_GH),
        ("copy", ["checkout_repo.sh", CHECKOUT_HOOK_PATH]),
        ("run", f"chmod 0755 {CHECKOUT_HOOK_PATH}"),
        ("run", WORKER_DIRS),
    ]


def build_recipe(
    base_image: str, version: str, b2sum: str, arch: str, *, computer_use: bool = False
) -> list[tuple[str, object]]:
    recipe: list[tuple[str, object]] = [("base", base_image), *cli_recipe(version, b2sum, arch)]
    if computer_use:
        recipe.append(("run", APT_DESKTOP))
        recipe.append(("run", CHROME_FIRST_RUN))
    return recipe


def computer_use_enabled() -> bool:
    return os.environ.get("WORKER_COMPUTER_USE", "").strip().lower() in ("1", "true", "yes", "on")


def default_base_image(computer_use: bool) -> str:
    override = os.environ.get("IMAGE_BASE", "").strip()
    if override:
        return override
    return DEFAULT_DESKTOP_BASE_IMAGE if computer_use else DEFAULT_BASE_IMAGE


def image_public() -> bool:
    """``IMAGE_PUBLIC=true`` registers the image so any namespace can run it."""
    return os.environ.get("IMAGE_PUBLIC", "").strip().lower() in ("1", "true", "yes", "on")


def recipe_sha8(recipe: list[tuple[str, object]]) -> str:
    payload = json.dumps(recipe, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def image_name(sha8: str) -> str:
    """Name for the image built from the current recipe.

    ``IMAGE_NAME`` wins only when it is a custom name. A stale recipe-hash pin,
    for example the minimal image left in ``.env`` after ``WORKER_COMPUTER_USE``
    was turned on, must not make the builder reuse the wrong image.
    """
    hashed = f"{DEFAULT_IMAGE_PREFIX}-{sha8}"
    override = os.environ.get("IMAGE_NAME", "").strip()
    if not override or override == hashed:
        return hashed
    if override.startswith(f"{DEFAULT_IMAGE_PREFIX}-"):
        print(f"IMAGE_NAME={override} is a stale recipe pin; the current recipe is {hashed}.")
        return hashed
    return override


def assemble_image(name: str, recipe: list[tuple[str, object]]) -> Image:
    base_image = next(value for kind, value in recipe if kind == "base")
    image = Image(name=name, base_image=str(base_image))
    for kind, value in recipe:
        if kind == "run":
            image = image.run(str(value))
        elif kind == "copy":
            src, dest = value  # type: ignore[misc]
            image = image.copy(str(src), str(dest))
        elif kind == "env":
            key, val = value  # type: ignore[misc]
            image = image.env(str(key), str(val))
        elif kind == "workdir":
            image = image.workdir(str(value))
    return image


def cli_settings() -> tuple[str, str, str]:
    version = os.environ.get("CURSOR_CLI_VERSION", "").strip() or DEFAULT_CLI_VERSION
    b2sum = os.environ.get("CURSOR_CLI_B2SUM", "").strip()
    if not b2sum and version == DEFAULT_CLI_VERSION:
        b2sum = DEFAULT_CLI_B2SUM
    arch = os.environ.get("CURSOR_CLI_ARCH", "").strip() or DEFAULT_CLI_ARCH
    return version, b2sum, arch


def describe_base_image(base_image: str, computer_use: bool) -> str:
    """One line that says which base the worker image starts from, and why."""
    if os.environ.get("IMAGE_BASE", "").strip():
        return f"{base_image} (set by IMAGE_BASE)"
    if computer_use:
        return f"{base_image} (Ubuntu desktop with a browser; WORKER_COMPUTER_USE=true)"
    return f"{base_image} (minimal Ubuntu, no desktop)"


def ensure_worker_image(*, verbose: bool = True, indent: str = "") -> str:
    """Return the worker image name, building it when it is not registered yet.

    ``indent`` prefixes every progress line, so a caller that prints its own
    step header can nest these lines under it.
    """
    computer_use = computer_use_enabled()
    base_image = default_base_image(computer_use)
    version, b2sum, arch = cli_settings()
    recipe = build_recipe(base_image, version, b2sum, arch, computer_use=computer_use)
    sha8 = recipe_sha8(recipe)
    name = image_name(sha8)

    def note(text: str) -> None:
        print(f"{indent}{text}", flush=True)

    note(f"Base: {describe_base_image(base_image, computer_use)}")
    note(f"Cursor CLI: {version} ({arch}, lab channel)")
    note(f"Name: {name} (suffix {sha8} = hash of the build recipe)")

    if find_sandbox_image_by_name(name) is not None:
        note("Already registered on Tensorlake. Nothing to build.")
        return name

    cpus = float(os.environ.get("SANDBOX_CPUS", "") or DEFAULT_CPU)
    memory_mb = int(os.environ.get("SANDBOX_MEMORY_MB", "") or DEFAULT_MEMORY_MB)
    disk_mb = int(os.environ.get("SANDBOX_DISK_MB", "") or DEFAULT_DISK_MB)

    image = assemble_image(name, recipe)
    note("Not registered yet. Building it (a few minutes)...")
    image.build(
        registered_name=name,
        cpus=cpus,
        memory_mb=memory_mb,
        disk_mb=disk_mb,
        is_public=image_public(),
        context_dir=str(PACKAGE_DIR),
        verbose=verbose,
    )
    note(f"Built and registered: {name}")
    return name


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    load_dotenv_if_available()
    try:
        name = ensure_worker_image()
    except Exception as exc:
        print(f"Image build failed: {exc}", file=sys.stderr)
        return 1
    print(f"Set IMAGE_NAME={name} in .env before you launch the orchestrator.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
