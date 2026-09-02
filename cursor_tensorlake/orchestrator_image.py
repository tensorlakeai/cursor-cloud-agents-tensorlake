"""Build the Tensorlake image that runs the orchestrator inside a sandbox.

The orchestrator sandbox runs ``agent worker controller`` (so it needs the same
pinned Cursor CLI as the workers) and this Python package (so the controller can
exec ``cursor-tl-spawn``). The image is built from the base image with the shared
CLI recipe plus pip, then installs the package from the repo checkout.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from tensorlake import Image, find_sandbox_image_by_name

from .config import CHECKOUT_HOOK_PATH, load_dotenv_if_available
from .worker_image import (
    DEFAULT_BASE_IMAGE,
    assemble_image,
    cli_recipe,
    cli_settings,
    image_public,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_ORCHESTRATOR_IMAGE_NAME = "cursor-tl-orchestrator"
INSTALL_DIR = "/opt/cursor-tensorlake"

# Repo root holds pyproject.toml and the package; it is the build context.
REPO_ROOT = Path(__file__).resolve().parent.parent

# Used when this package runs from site-packages (uvx, pip) and no repo checkout
# is around to copy into the image.
DEFAULT_PACKAGE_SOURCE = "git+https://github.com/tensorlakeai/cursor-cloud-agents-tensorlake"

APT_PYTHON = (
    "export DEBIAN_FRONTEND=noninteractive && apt-get update && "
    "apt-get install -y --no-install-recommends python3-pip && "
    "rm -rf /var/lib/apt/lists/*"
)


def orchestrator_image_name() -> str:
    return os.environ.get("ORCHESTRATOR_IMAGE_NAME", "").strip() or DEFAULT_ORCHESTRATOR_IMAGE_NAME


def orchestrator_base_image() -> str:
    """The orchestrator never needs a desktop, so it ignores IMAGE_BASE."""
    return os.environ.get("ORCHESTRATOR_IMAGE_BASE", "").strip() or DEFAULT_BASE_IMAGE


def repo_checkout_available() -> bool:
    return (REPO_ROOT / "pyproject.toml").is_file() and (REPO_ROOT / "cursor_tensorlake").is_dir()


def orchestrator_recipe(
    base_image: str, version: str, b2sum: str, arch: str, *, from_checkout: bool = True
) -> list[tuple[str, object]]:
    recipe: list[tuple[str, object]] = [("base", base_image)]
    for kind, value in cli_recipe(version, b2sum, arch):
        if kind == "copy":
            if not from_checkout:
                # No build context: the hook is installed from the pip package below.
                continue
            # Context is the repo root here, not the package directory.
            src, dest = value  # type: ignore[misc]
            recipe.append(("copy", [f"cursor_tensorlake/{src}", dest]))
        else:
            recipe.append((kind, value))
    recipe.append(("run", APT_PYTHON))
    if from_checkout:
        recipe.extend(
            [
                ("copy", ["pyproject.toml", f"{INSTALL_DIR}/pyproject.toml"]),
                ("copy", ["README.md", f"{INSTALL_DIR}/README.md"]),
                ("copy", ["cursor_tensorlake", f"{INSTALL_DIR}/cursor_tensorlake"]),
                ("run", f"pip install --break-system-packages {INSTALL_DIR}"),
                ("workdir", INSTALL_DIR),
            ]
        )
    else:
        source = os.environ.get("PACKAGE_SOURCE", "").strip() or DEFAULT_PACKAGE_SOURCE
        hook_src = (
            "$(python3 -c 'import cursor_tensorlake, os; "
            "print(os.path.join(os.path.dirname(cursor_tensorlake.__file__), \"checkout_repo.sh\"))')"
        )
        recipe.extend(
            [
                ("run", f"pip install --break-system-packages '{source}'"),
                ("run", f'install -m 0755 "{hook_src}" {CHECKOUT_HOOK_PATH}'),
                ("workdir", "/home/tl-user"),
            ]
        )
    return recipe


def ensure_orchestrator_image(*, rebuild: bool = True, verbose: bool = True, indent: str = "") -> str:
    """Return the orchestrator image name; build it when missing or when asked.

    The image pip-installs this package, so a code change needs ``rebuild=True``.
    ``indent`` prefixes every progress line, so a caller that prints its own
    step header can nest these lines under it.
    """
    base_image = orchestrator_base_image()
    version, b2sum, arch = cli_settings()
    name = orchestrator_image_name()
    from_checkout = repo_checkout_available()
    recipe = orchestrator_recipe(base_image, version, b2sum, arch, from_checkout=from_checkout)

    def note(text: str) -> None:
        print(f"{indent}{text}", flush=True)

    note(f"Base: {base_image} (minimal Ubuntu; the controller needs no desktop)")
    note(f"Cursor CLI: {version} ({arch}, lab channel)")
    note(f"Name: {name}")

    if not rebuild and find_sandbox_image_by_name(name) is not None:
        note("Already registered on Tensorlake. Nothing to build.")
        return name

    if rebuild:
        note("Rebuild requested (--rebuild).")
    else:
        note("Not registered yet.")
    if from_checkout:
        note(f"Build context: {REPO_ROOT}")
    else:
        note("No repo checkout found; the image installs the package from PACKAGE_SOURCE.")

    image: Image = assemble_image(name, recipe)
    note("Building it (a few minutes)...")
    image.build(
        registered_name=name,
        cpus=float(os.environ.get("ORCHESTRATOR_CPUS", "") or 1.0),
        memory_mb=int(os.environ.get("ORCHESTRATOR_MEMORY_MB", "") or 2048),
        disk_mb=int(os.environ.get("ORCHESTRATOR_DISK_MB", "") or 20480),
        is_public=image_public(),
        context_dir=str(REPO_ROOT) if from_checkout else None,
        verbose=verbose,
    )
    note(f"Built and registered: {name}")
    return name


def main() -> int:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    load_dotenv_if_available()
    try:
        ensure_orchestrator_image(rebuild=True)
    except Exception as exc:
        print(f"Orchestrator image build failed: {exc}", file=sys.stderr)
        return 1
    print("Launch it with: cursor-tl-orchestrator-sandbox")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
