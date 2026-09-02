.PHONY: install test up up-computer-use build build-orchestrator pool orchestrator-sandbox status logs stop

# --- Setup ---

install:
	uv sync --all-extras

test:
	uv run pytest -q

# One command: keys -> pool -> images -> orchestrator sandbox -> watching.
up:
	uv run cursor-tl-up

# Same, with a desktop worker image and --computer-use workers.
up-computer-use:
	uv run cursor-tl-up --computer-use

# Register the Cursor pool with a reconnect window for hibernated workers.
pool:
	uv run cursor-tl-pool register

# Build the per-session worker image (the microVM each Cursor worker runs in).
build:
	uv run cursor-tl-build-image

# --- The orchestrator runs inside a Tensorlake sandbox ---

# Build the orchestrator image that carries this package and the Cursor CLI.
build-orchestrator:
	uv run cursor-tl-build-orchestrator-image

# Launch (or resume) the orchestrator sandbox and ensure the controller runs.
# Idempotent: safe to schedule on a cron to keep it watching.
orchestrator-sandbox:
	uv run cursor-tl-orchestrator-sandbox

status:
	uv run cursor-tl-orchestrator-sandbox --status

logs:
	uv run cursor-tl-orchestrator-sandbox --logs

stop:
	uv run cursor-tl-orchestrator-sandbox --terminate
