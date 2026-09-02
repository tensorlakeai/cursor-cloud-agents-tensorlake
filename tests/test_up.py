"""cursor-tl-up: .env handling, pool detection, and the step sequence."""

from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from pathlib import Path

from cursor_tensorlake import up
from cursor_tensorlake.config import ConfigError
from cursor_tensorlake.cursor_api import CursorAPIError


def _args(**overrides):
    base = dict(
        non_interactive=False, rebuild=False, computer_use=False, ready_timeout=900, no_wait=False,
        repo_url=None, any_repo=False, restart=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class EnvFileTests(unittest.TestCase):
    def test_replace_and_append_keep_comments(self) -> None:
        path = Path(tempfile.mkdtemp()) / ".env"
        path.write_text("# keys\nCURSOR_API_KEY=old\nIMAGE_NAME=\n")
        up.update_env_file(path, {"CURSOR_API_KEY": "new", "IMAGE_NAME": "img-1", "CURSOR_POOL": "p"})
        text = path.read_text()
        self.assertEqual(text, "# keys\nCURSOR_API_KEY=new\nIMAGE_NAME=img-1\nCURSOR_POOL=p\n")
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")

    def test_collect_missing_asks_only_for_gaps(self) -> None:
        asked: list[str] = []

        def secret(prompt: str) -> str:
            asked.append(prompt)
            return "sa_x"

        updates = up.collect_missing(
            {"TENSORLAKE_API_KEY": "tl_x", "CURSOR_API_KEY": "replace-with-cursor-service-account-key"},
            interactive=True,
            ask_secret=secret,
            ask=lambda _p: "",
        )
        self.assertEqual(updates, {"CURSOR_API_KEY": "sa_x", "CURSOR_POOL": "tensorlake"})
        self.assertEqual(len(asked), 1)

    def test_non_interactive_fails_on_gap(self) -> None:
        with self.assertRaises(ConfigError):
            up.collect_missing({"TENSORLAKE_API_KEY": "tl_x"}, interactive=False)


class PoolTests(unittest.TestCase):
    def test_pool_names_shapes(self) -> None:
        self.assertEqual(up.pool_names({"pools": [{"poolName": "a"}, {"name": "b"}, "c"]}), {"a", "b", "c"})
        self.assertEqual(up.pool_names([{"pool": "z"}]), {"z"})
        self.assertEqual(up.pool_names({"unexpected": 1}), set())

    def test_ensure_pool_registers_once(self) -> None:
        class API:
            def __init__(self) -> None:
                self.registered: list[str] = []

            def list_pools(self):
                return {"pools": [{"poolName": "old"}]}

            def register_pool(self, name, *, worker_ready_timeout_seconds, repo_url=None):
                self.registered.append(name)
                return {}

        api = API()
        self.assertEqual(up.ensure_pool(api, "old", ready_timeout=900), "exists")
        self.assertEqual(up.ensure_pool(api, "new", ready_timeout=900), "registered")
        self.assertEqual(api.registered, ["new"])

    def test_ensure_pool_with_repo_ignores_any_repo_row(self) -> None:
        class API:
            def __init__(self) -> None:
                self.registered: list[tuple[str, str | None]] = []

            def list_pools(self):
                return {"pools": [
                    {"poolName": "tensorlake"},
                    {"poolName": "bound", "repoOwner": "Acme", "repoName": "Widgets",
                     "repoUrl": "https://github.com/Acme/Widgets"},
                ]}

            def register_pool(self, name, *, worker_ready_timeout_seconds, repo_url=None):
                self.registered.append((name, repo_url))
                return {}

        api = API()
        repo = "https://github.com/acme/widgets"
        # An any-repo row does not satisfy a repo-bound pool.
        self.assertEqual(up.ensure_pool(api, "tensorlake", ready_timeout=900, repo_url=repo), "registered")
        # A row bound to the same repository, in any spelling, does.
        self.assertEqual(up.ensure_pool(api, "bound", ready_timeout=900, repo_url=repo + ".git"), "exists")
        # Without a repo, any row with the name counts.
        self.assertEqual(up.ensure_pool(api, "tensorlake", ready_timeout=900), "exists")
        self.assertEqual(api.registered, [("tensorlake", repo)])

    def test_ensure_pool_any_repo_needs_the_any_repo_row(self) -> None:
        class API:
            def __init__(self) -> None:
                self.registered: list[tuple[str, str | None]] = []

            def list_pools(self):
                return {"pools": [
                    {"poolName": "tensorlake", "repoOwner": "acme", "repoName": "widgets",
                     "repoUrl": "https://github.com/acme/widgets"},
                    {"poolName": "open"},
                ]}

            def register_pool(self, name, *, worker_ready_timeout_seconds, repo_url=None):
                self.registered.append((name, repo_url))
                return {}

        api = API()
        # A repo-bound row does not satisfy an any-repo pool.
        self.assertEqual(up.ensure_pool(api, "tensorlake", ready_timeout=900, any_repo=True), "registered")
        self.assertEqual(up.ensure_pool(api, "open", ready_timeout=900, any_repo=True), "exists")
        self.assertEqual(api.registered, [("tensorlake", None)])

    def test_ensure_pool_treats_conflict_as_exists(self) -> None:
        class API:
            def list_pools(self):
                return []

            def register_pool(self, *a, **k):
                raise CursorAPIError("dup", status_code=409)

        self.assertEqual(up.ensure_pool(API(), "p", ready_timeout=900), "exists")


class RunUpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_environ = dict(os.environ)
        self.addCleanup(self._restore_environ)
        self.tmp = Path(tempfile.mkdtemp())
        self.env_path = self.tmp / ".env"
        self.env_path.write_text("TENSORLAKE_API_KEY=tl_x\n")
        os.environ["TENSORLAKE_API_KEY"] = "tl_x"
        os.environ["STATE_DIR"] = str(self.tmp / "state")
        self.calls: list[str] = []

    def _restore_environ(self) -> None:
        os.environ.clear()
        os.environ.update(self._saved_environ)

    def _api_factory(self, key: str, url: str):
        calls = self.calls

        class API:
            def list_pools(self):
                calls.append("list_pools")
                return {"pools": []}

            def register_pool(self, name, *, worker_ready_timeout_seconds, repo_url=None):
                calls.append(f"register:{name}:{worker_ready_timeout_seconds}:{repo_url}")
                return {}

            def close(self):
                pass

        return API()

    def _run(self, args, statuses=None):
        import io

        statuses = list(statuses or [{"state": "watching"}])
        out = io.StringIO()
        code = up.run_up(
            args,
            env_path=self.env_path,
            ensure_worker_image=lambda: (self.calls.append("worker-image"), "cursor-tl-worker-deadbeef")[1],
            ensure_orchestrator_image=lambda rebuild: (self.calls.append(f"orch-image:{rebuild}"), "cursor-tl-orchestrator")[1],
            ensure_orchestrator=lambda config, restart=False, recreate=False: self.calls.append(
                f"launch:{config.image_name}" + (":restart" if restart else "") + (":recreate" if recreate else "")
            ),
            read_status=lambda config: statuses.pop(0) if statuses else None,
            api_factory=self._api_factory,
            ask_secret=lambda prompt: "sa_prompted",
            ask=lambda prompt: "",
            sleep=lambda s: None,
            out=out,
        )
        return code, out.getvalue()

    def test_full_sequence_writes_env(self) -> None:
        code, text = self._run(_args())
        self.assertEqual(code, 0, text)
        self.assertEqual(
            self.calls,
            ["list_pools", "register:tensorlake:900:None", "worker-image", "orch-image:False", "launch:cursor-tl-worker-deadbeef"],
        )
        self.assertIn("serves every repository", text)
        self.assertIn('Do not pick "Any repo"', text)
        env = self.env_path.read_text()
        self.assertIn("CURSOR_API_KEY=sa_prompted", env)
        self.assertIn("IMAGE_NAME=cursor-tl-worker-deadbeef", env)
        self.assertIn("CURSOR_POOL=tensorlake", env)
        self.assertIn("watching pool", text)

    def test_repo_url_lands_in_env_and_registration(self) -> None:
        code, text = self._run(_args(repo_url="https://github.com/acme/widgets.git"))
        self.assertEqual(code, 0, text)
        self.assertIn("CURSOR_POOL_REPO_URL=https://github.com/acme/widgets.git", self.env_path.read_text())
        self.assertIn("register:tensorlake:900:https://github.com/acme/widgets", self.calls)
        self.assertIn("bound to https://github.com/acme/widgets", text)
        self.assertNotIn("Caution", text)

    def test_computer_use_flag_lands_in_env(self) -> None:
        code, _ = self._run(_args(computer_use=True, rebuild=True))
        self.assertEqual(code, 0)
        self.assertIn("WORKER_COMPUTER_USE=true", self.env_path.read_text())
        self.assertIn("orch-image:True", self.calls)

    def test_heartbeat_timeout_returns_1(self) -> None:
        import unittest.mock as mock

        with mock.patch.object(up, "WATCH_TIMEOUT_SECS", 0.0):
            code, text = self._run(_args(), statuses=[None])
        self.assertEqual(code, 1)
        self.assertIn("Not ready yet", text)

    def test_bad_cursor_key(self) -> None:
        def factory(key, url):
            class API:
                def list_pools(self):
                    raise CursorAPIError("HTTP 401 Invalid User API Key", status_code=401)

                def close(self):
                    pass

            return API()

        import io

        out = io.StringIO()
        code = up.run_up(
            _args(), env_path=self.env_path,
            ensure_worker_image=lambda: "x", ensure_orchestrator_image=lambda rebuild: "y",
            ensure_orchestrator=lambda c, **kw: None, read_status=lambda c: None,
            api_factory=factory, ask_secret=lambda p: "sa_bad", ask=lambda p: "", out=out,
        )
        self.assertEqual(code, 1)
        self.assertIn("service-account", out.getvalue())

    def test_cursor_outage_is_not_blamed_on_the_key(self) -> None:
        def factory(key, url):
            class API:
                def list_pools(self):
                    raise CursorAPIError("GET /v0/private-workers/pools failed: ReadTimeout")

                def close(self):
                    pass

            return API()

        import io

        out = io.StringIO()
        code = up.run_up(
            _args(), env_path=self.env_path,
            ensure_worker_image=lambda: "x", ensure_orchestrator_image=lambda rebuild: "y",
            ensure_orchestrator=lambda c, **kw: None, read_status=lambda c: None,
            api_factory=factory, ask_secret=lambda p: "sa_ok", ask=lambda p: "", out=out,
        )
        self.assertEqual(code, 1)
        self.assertIn("did not answer", out.getvalue())
        self.assertNotIn("rejected the key", out.getvalue())

    def test_restart_flag_reaches_the_launcher(self) -> None:
        code, text = self._run(_args(restart=True))
        self.assertEqual(code, 0, text)
        self.assertIn("launch:cursor-tl-worker-deadbeef:restart", self.calls)

    def test_rebuild_recreates_the_orchestrator_sandbox(self) -> None:
        code, text = self._run(_args(rebuild=True))
        self.assertEqual(code, 0, text)
        self.assertIn("orch-image:True", self.calls)
        self.assertIn("launch:cursor-tl-worker-deadbeef:recreate", self.calls)

    def test_any_repo_lands_in_env_and_registration(self) -> None:
        code, text = self._run(_args(any_repo=True))
        self.assertEqual(code, 0, text)
        self.assertIn("register:tensorlake:900:None", self.calls)
        self.assertIn("CURSOR_POOL_MODE=any-repo", self.env_path.read_text())
        self.assertIn("workers clone on claim", text)
        self.assertIn('pick "Any repo"', text)
        self.assertIn('agent "Clone https://github.com/org/repo', text)
        self.assertNotIn("--repo https://", text)
        self.assertNotIn("Do not pick", text)

    def test_any_repo_clears_a_saved_repo_pin(self) -> None:
        self.env_path.write_text("TENSORLAKE_API_KEY=tl_x\nCURSOR_POOL_REPO_URL=https://github.com/acme/widgets\n")
        code, text = self._run(_args(any_repo=True))
        self.assertEqual(code, 0, text)
        env = self.env_path.read_text()
        self.assertIn("CURSOR_POOL_REPO_URL=\n", env)
        self.assertIn("CURSOR_POOL_MODE=any-repo", env)

    def test_repo_url_and_any_repo_exclude_each_other(self) -> None:
        with self.assertRaises(ConfigError):
            self._run(_args(any_repo=True, repo_url="https://github.com/acme/widgets"))


if __name__ == "__main__":
    unittest.main()
