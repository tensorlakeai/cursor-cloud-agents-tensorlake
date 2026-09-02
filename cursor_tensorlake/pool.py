"""Manage the Cursor pool and inspect its queue from the command line.

    cursor-tl-pool register [--name P] [--ready-timeout 900] [--repo-url URL]
    cursor-tl-pool deregister [--name P] [--repo-url URL]
    cursor-tl-pool list
    cursor-tl-pool summary
    cursor-tl-pool workers
    cursor-tl-pool pending
    cursor-tl-pool release <request-id>
    cursor-tl-pool agent "prompt text" [--repo https://github.com/org/repo]

``register`` sets ``workerReadyTimeoutSeconds``: how long Cursor waits for a
hibernated worker to reconnect before it hands a follow-up to another worker.
``deregister`` without ``--repo-url`` removes the any-repo row of that name.
A controller started without ``--repository`` creates that row; only workers
with no ``origin`` (``CURSOR_POOL_MODE=any-repo``) join it.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .config import Config, ConfigError, load_dotenv_if_available
from .cursor_api import CursorAPI, CursorAPIError

DEFAULT_READY_TIMEOUT = 900


def _print(data: object) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv_if_available()
    parser = argparse.ArgumentParser(prog="cursor-tl-pool", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    reg = sub.add_parser("register", help="register (or update) the pool")
    reg.add_argument("--name", help="pool name (default: CURSOR_POOL)")
    reg.add_argument("--ready-timeout", type=int, default=DEFAULT_READY_TIMEOUT,
                     help="seconds Cursor waits for a hibernated worker to reconnect")
    reg.add_argument("--repo-url", help="bind the pool to one repository (default: CURSOR_POOL_REPO_URL, else any repo)")

    dereg = sub.add_parser("deregister", help="remove one pool row; without --repo-url, the any-repo row")
    dereg.add_argument("--name", help="pool name (default: CURSOR_POOL)")
    dereg.add_argument("--repo-url", help="remove the row bound to this repository instead")

    sub.add_parser("list", help="list team pools")
    sub.add_parser("summary", help="connected and idle worker counts")
    sub.add_parser("workers", help="list workers")
    sub.add_parser("pending", help="list pending requests for the pool")
    rel = sub.add_parser("release", help="release a claim so the request re-queues")
    rel.add_argument("request_id")
    agent = sub.add_parser("agent", help="launch an agent into the pool (smoke test)")
    agent.add_argument("prompt")
    agent.add_argument("--repo", help="HTTPS repository URL")

    args = parser.parse_args(argv)
    try:
        config = Config.from_env(require_image=False)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    api = CursorAPI(config.cursor_api_key, config.cursor_api_url)
    try:
        if args.command == "register":
            name = args.name or config.cursor_pool
            repo_url = args.repo_url or config.cursor_pool_repo_url
            _print(api.register_pool(name, worker_ready_timeout_seconds=args.ready_timeout, repo_url=repo_url))
            bound = f"bound to {repo_url}" if repo_url else "for any repository"
            print(f"Pool {name!r} registered {bound}, workerReadyTimeoutSeconds={args.ready_timeout}.", file=sys.stderr)
        elif args.command == "deregister":
            name = args.name or config.cursor_pool
            _print(api.deregister_pool(name, repo_url=args.repo_url))
            which = f"row bound to {args.repo_url}" if args.repo_url else "any-repo row"
            print(f"Removed the {which} of pool {name!r}.", file=sys.stderr)
        elif args.command == "list":
            _print(api.list_pools())
        elif args.command == "summary":
            _print(api.summary())
        elif args.command == "workers":
            _print(api.list_workers())
        elif args.command == "pending":
            _print(api.list_pending_requests(pool=config.cursor_pool))
        elif args.command == "release":
            api.release_claim(args.request_id)
            print(f"Released claim {args.request_id}.")
        elif args.command == "agent":
            _print(api.create_agent(args.prompt, config.cursor_pool, repo_url=args.repo))
    except CursorAPIError as exc:
        print(f"Cursor API error: {config.redacted(exc)}", file=sys.stderr)
        return 1
    finally:
        api.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
