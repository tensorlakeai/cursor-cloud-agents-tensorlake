from __future__ import annotations

import os
import unittest
from pathlib import Path

from cursor_tensorlake.config import (
    AGENT_BIN,
    CHECKOUT_HOOK_PATH,
    Claim,
    ConfigError,
    load_dotenv_if_available,
    redact,
    worker_command,
    worker_environment,
)

from ._fakes import make_claim, make_config


class ConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        config, _ = make_config()
        self.assertEqual(config.cursor_pool, "tensorlake")
        self.assertEqual(config.worker_idle_release_secs, 300)
        self.assertEqual(config.session_retention_secs, 86400)
        self.assertFalse(config.wake_offline_claims)
        self.assertEqual(config.sandbox_allow_out, ())

    def test_pool_repo_url_normalized(self) -> None:
        config, _ = make_config()
        self.assertIsNone(config.cursor_pool_repo_url)
        config, _ = make_config(CURSOR_POOL_REPO_URL=" https://github.com/Acme/Widgets.git/ ")
        self.assertEqual(config.cursor_pool_repo_url, "https://github.com/Acme/Widgets")

    def test_pool_repo_url_rejects_non_https_or_partial(self) -> None:
        for bad in ("git@github.com:acme/widgets.git", "https://github.com/acme", "acme/widgets"):
            with self.assertRaises(ConfigError, msg=bad):
                make_config(CURSOR_POOL_REPO_URL=bad)

    def test_missing_required(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            make_config(CURSOR_API_KEY="")
        self.assertIn("CURSOR_API_KEY", str(ctx.exception))

    def test_my_machines_needs_no_service_account_key(self) -> None:
        config, _ = make_config(
            CURSOR_API_KEY="",
            CURSOR_USER_API_KEY="key_personal",
            from_env_kwargs={"require_cursor_key": False},
        )
        self.assertEqual(config.cursor_api_key, "")
        self.assertEqual(config.cursor_user_api_key, "key_personal")
        self.assertIn("key_personal", config.secrets_for_redaction())

    def test_allow_out_must_include_cursor_hosts(self) -> None:
        with self.assertRaises(ConfigError):
            make_config(SANDBOX_ALLOW_OUT="github.com")
        config, _ = make_config(SANDBOX_ALLOW_OUT="api2.cursor.sh,api2direct.cursor.sh,github.com")
        self.assertIn("github.com", config.sandbox_allow_out)

    def test_labels_json_validated(self) -> None:
        with self.assertRaises(ConfigError):
            make_config(WORKER_LABELS_JSON="[1,2]")
        config, _ = make_config(WORKER_LABELS_JSON='{"env": "prod"}')
        self.assertEqual(config.worker_labels_json, '{"env":"prod"}')

    def test_redaction(self) -> None:
        config, _ = make_config()
        text = "key sa_dummy_cursor_key and https://u:p@host/x"
        self.assertNotIn("sa_dummy_cursor_key", config.redacted(text))
        self.assertIn("https://<redacted>@host/x", redact(text, ()))


class DotenvTests(unittest.TestCase):
    """``.env`` is the source of truth; the shell must not pick the Tensorlake project."""

    def test_env_file_wins_over_shell(self) -> None:
        # conftest runs each test in an empty temp directory.
        Path(".env").write_text(
            "TENSORLAKE_API_KEY=tl_from_env\nTENSORLAKE_PROJECT_ID=\nCURSOR_POOL=tensorlake\n",
            encoding="utf-8",
        )
        os.environ["TENSORLAKE_API_KEY"] = "tl_from_ide_shell"
        os.environ["TENSORLAKE_PROJECT_ID"] = "proj_from_ide_shell"
        os.environ["LOG_LEVEL"] = "DEBUG"
        load_dotenv_if_available()
        self.assertEqual(os.environ["TENSORLAKE_API_KEY"], "tl_from_env")
        self.assertNotIn("TENSORLAKE_PROJECT_ID", os.environ, "blank in .env clears the shell value")
        self.assertEqual(os.environ["CURSOR_POOL"], "tensorlake")
        self.assertEqual(os.environ["LOG_LEVEL"], "DEBUG", "keys absent from .env keep the shell value")

    def test_no_env_file_keeps_shell(self) -> None:
        os.environ["TENSORLAKE_API_KEY"] = "tl_shell"
        load_dotenv_if_available()
        self.assertEqual(os.environ["TENSORLAKE_API_KEY"], "tl_shell")

    def test_sdk_calls_get_the_key_explicitly(self) -> None:
        config, _ = make_config(TENSORLAKE_PROJECT_ID="proj_1")
        os.environ["TENSORLAKE_API_KEY"] = "tl_dummy_key"
        try:
            self.assertEqual(
                config.tensorlake_kwargs(), {"api_key": "tl_dummy_key", "project_id": "proj_1"}
            )
        finally:
            del os.environ["TENSORLAKE_API_KEY"]


class ClaimTests(unittest.TestCase):
    def test_requires_worker_id_and_pool(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            Claim.from_env({"CURSOR_POOL": "p"})
        self.assertIn("CURSOR_AGENT_WORKER_ID", str(ctx.exception))

    def test_request_id_optional_for_warm_mode(self) -> None:
        claim = Claim.from_env({"CURSOR_AGENT_WORKER_ID": "w", "CURSOR_POOL": "p"})
        self.assertIsNone(claim.request_id)
        self.assertEqual(claim.repo_urls, ())
        self.assertIsNone(claim.primary_origin_url())

    def test_scheme_less_repo_gets_https(self) -> None:
        claim = make_claim()
        self.assertEqual(claim.primary_origin_url(), "https://github.com/acme/widgets")

    def test_repo_urls_json_wins(self) -> None:
        claim = make_claim(CURSOR_REPO_URLS='["github.com/a/b", "github.com/c/d"]')
        self.assertEqual(claim.repo_urls, ("github.com/a/b", "github.com/c/d"))
        self.assertEqual(claim.primary_origin_url(), "https://github.com/a/b")

    def test_extra_worker_dirs_one_root_per_further_repo(self) -> None:
        claim = make_claim(
            CURSOR_REPO_URLS='["github.com/acme/app", "github.com/acme/infra.git", "https://github.com/other/app", "github.com/acme/app"]'
        )
        self.assertEqual(
            claim.origin_urls(),
            ("https://github.com/acme/app", "https://github.com/acme/infra.git", "https://github.com/other/app"),
        )
        self.assertEqual(
            claim.extra_worker_dirs(),
            (
                ("/home/tl-user/repos/infra", "https://github.com/acme/infra.git"),
                ("/home/tl-user/repos/other-app", "https://github.com/other/app"),
            ),
        )
        self.assertEqual(make_claim().extra_worker_dirs(), ())

    def test_one_repository_spelled_two_ways_gets_one_root(self) -> None:
        # The checkout hook matches a payload URL to a root without the `.git`
        # suffix, so two spellings must not become two roots.
        claim = make_claim(
            CURSOR_REPO_URLS='["github.com/acme/app", "github.com/acme/infra", "https://GitHub.com/acme/infra.GIT"]'
        )
        self.assertEqual(
            claim.origin_urls(),
            ("https://github.com/acme/app", "https://github.com/acme/infra"),
        )
        self.assertEqual(
            claim.extra_worker_dirs(), (("/home/tl-user/repos/infra", "https://github.com/acme/infra"),)
        )

    def test_bad_url_after_the_primary_is_dropped(self) -> None:
        # A request the worker can still serve must not fail on a repository
        # that is not the primary one.
        claim = make_claim(
            CURSOR_REPO_URLS='["github.com/acme/app", "git@github.com:acme/infra.git"]'
        )
        with self.assertLogs("cursor_tensorlake.config", "WARNING") as logs:
            self.assertEqual(claim.origin_urls(), ("https://github.com/acme/app",))
        self.assertIn("repository 2", logs.output[0])
        self.assertNotIn("infra", logs.output[0])
        self.assertEqual(claim.extra_worker_dirs(), ())

    def test_extra_roots_retain_positions_after_filtering(self) -> None:
        claim = make_claim(
            CURSOR_REPO_URLS='["github.com/acme/app", "git@github.com:acme/bad.git", "github.com/acme/infra", "github.com/acme/infra.git", "github.com/acme/docs"]'
        )
        with self.assertLogs("cursor_tensorlake.config", "WARNING"):
            self.assertEqual(
                claim.extra_worker_roots(),
                (
                    (2, "/home/tl-user/repos/infra", "https://github.com/acme/infra"),
                    (4, "/home/tl-user/repos/docs", "https://github.com/acme/docs"),
                ),
            )

    def test_rejects_credentialed_or_ssh_urls(self) -> None:
        with self.assertRaises(ConfigError):
            make_claim(CURSOR_REPO_URL="https://user:tok@github.com/a/b").primary_origin_url()
        with self.assertRaises(ConfigError):
            make_claim(CURSOR_REPO_URL="git@github.com:a/b.git").primary_origin_url()


class WorkerCommandTests(unittest.TestCase):
    def test_flags_precede_start(self) -> None:
        config, _ = make_config(WORKER_LABELS_JSON='{"team":"x"}')
        argv = worker_command(config, make_claim(), has_repo=True)
        self.assertEqual(argv[0], AGENT_BIN)
        self.assertEqual(argv[1], "worker")
        self.assertEqual(argv[-2:], ["start", "--verbose"])
        start = argv.index("start")
        for flag in ("--pool", "--worker-dir", "--idle-release-timeout", "--mint-github-token", "--on-session-start", "--labels-file", "--name"):
            self.assertIn(flag, argv[:start], flag)
        self.assertEqual(argv[argv.index("--on-session-start") + 1], CHECKOUT_HOOK_PATH)
        self.assertEqual(argv[argv.index("--idle-release-timeout") + 1], "300")

    def test_extra_dirs_repeat_worker_dir_after_the_primary(self) -> None:
        config, _ = make_config()
        argv = worker_command(config, make_claim(), has_repo=True, extra_dirs=["/home/tl-user/repos/infra"])
        dirs = [argv[i + 1] for i, flag in enumerate(argv) if flag == "--worker-dir"]
        self.assertEqual(dirs, ["/home/tl-user/workspace", "/home/tl-user/repos/infra"])
        self.assertLess(argv.index("--worker-dir"), argv.index("--management-addr"))

    def test_worker_id_travels_in_env_not_argv(self) -> None:
        config, _ = make_config()
        argv = worker_command(config, make_claim(), has_repo=True)
        self.assertNotIn("--worker-id", argv)
        self.assertEqual(worker_environment(config, make_claim())["CURSOR_AGENT_WORKER_ID"], "worker-abc123")

    def test_wake_claim(self) -> None:
        claim = make_claim(CURSOR_WAKE="1", CURSOR_WAKE_TIMEOUT_MS="840000")
        self.assertTrue(claim.wake)
        self.assertEqual(claim.wake_timeout_ms, 840000)
        self.assertFalse(make_claim().wake)

    def test_no_repo_omits_minting(self) -> None:
        config, _ = make_config()
        argv = worker_command(config, make_claim(), has_repo=False)
        self.assertNotIn("--mint-github-token", argv)
        self.assertNotIn("--labels-file", argv)

    def test_computer_use_flags(self) -> None:
        config, _ = make_config(WORKER_COMPUTER_USE="true", WORKER_SHARE_DESKTOP="view")
        argv = worker_command(config, make_claim(), has_repo=True)
        start = argv.index("start")
        self.assertIn("--computer-use", argv[:start])
        self.assertEqual(argv[argv.index("--share-desktop") + 1], "view")
        plain, _ = make_config()
        self.assertNotIn("--computer-use", worker_command(plain, make_claim(), has_repo=True))
        self.assertNotIn("--share-desktop", worker_command(plain, make_claim(), has_repo=True))

    def test_my_machine_worker_gets_computer_use_flags(self) -> None:
        from cursor_tensorlake.my_machine import machine_worker_command

        config, _ = make_config(WORKER_COMPUTER_USE="true", WORKER_SHARE_DESKTOP="view")
        argv = machine_worker_command(config, "tl-demo", ["/home/tl-user/workspace/app"])
        start = argv.index("start")
        self.assertIn("--computer-use", argv[:start])
        self.assertEqual(argv[argv.index("--share-desktop") + 1], "view")
        self.assertEqual(argv[argv.index("--worker-dir") + 1], "/home/tl-user/workspace/app")
        plain, _ = make_config()
        self.assertNotIn("--computer-use", machine_worker_command(plain, "tl-demo", []))

    def test_computer_use_pins_the_display_the_image_already_runs(self) -> None:
        # The desktop worker image boots TigerVNC + Xfce on :1 before any
        # worker starts. Attaching to it keeps one desktop per sandbox, at a
        # number an outside recorder can film.
        config, _ = make_config(WORKER_COMPUTER_USE="true")
        argv = worker_command(config, make_claim(), has_repo=True)
        self.assertEqual(argv[argv.index("--display") + 1], ":1")
        env = worker_environment(config, make_claim())
        self.assertEqual(env["DISPLAY"], ":1")
        self.assertEqual(env["XAUTHORITY"], "/home/tl-user/.Xauthority")

    def test_share_desktop_leaves_the_desktop_to_the_worker(self) -> None:
        # --share-desktop shares a worker-created, isolated desktop, so the
        # default display pin steps aside for it.
        config, _ = make_config(WORKER_COMPUTER_USE="true", WORKER_SHARE_DESKTOP="view")
        self.assertIsNone(config.worker_display)
        self.assertNotIn("--display", worker_command(config, make_claim(), has_repo=True))
        self.assertNotIn("DISPLAY", worker_environment(config, make_claim()))

    def test_explicit_display_wins_over_share_desktop(self) -> None:
        config, _ = make_config(
            WORKER_COMPUTER_USE="true", WORKER_SHARE_DESKTOP="view", WORKER_DISPLAY=":2"
        )
        self.assertEqual(config.worker_display, ":2")

    def test_managed_display_asks_the_worker_for_its_own_desktop(self) -> None:
        config, _ = make_config(WORKER_COMPUTER_USE="true", WORKER_DISPLAY="managed")
        self.assertIsNone(config.worker_display)
        self.assertIn("--computer-use", worker_command(config, make_claim(), has_repo=True))
        self.assertNotIn("--display", worker_command(config, make_claim(), has_repo=True))

    def test_display_validated(self) -> None:
        with self.assertRaises(ConfigError):
            make_config(WORKER_DISPLAY="screen one")

    def test_no_computer_use_exports_no_display(self) -> None:
        config, _ = make_config()
        self.assertNotIn("DISPLAY", worker_environment(config, make_claim()))

    def test_my_machine_worker_shell_gets_the_display(self) -> None:
        # Same gap as the pool worker: Cursor's executor reads --display, but a
        # browser the agent starts from a shell reads DISPLAY.
        from cursor_tensorlake.config import desktop_environment

        config, _ = make_config(WORKER_COMPUTER_USE="true")
        self.assertEqual(desktop_environment(config)["DISPLAY"], ":1")
        plain, _ = make_config()
        self.assertEqual(desktop_environment(plain), {})

    def test_max_workers(self) -> None:
        config, _ = make_config(MAX_WORKERS="20")
        self.assertEqual(config.max_workers, 20)
        with self.assertRaises(ConfigError):
            make_config(MAX_WORKERS="-1")

    def test_share_desktop_validated(self) -> None:
        from cursor_tensorlake.config import ConfigError
        with self.assertRaises(ConfigError):
            make_config(WORKER_SHARE_DESKTOP="everyone")
        config, _ = make_config(WORKER_SHARE_DESKTOP="true")
        self.assertEqual(config.worker_share_desktop, "view_and_control")

    def test_environment_allowlist(self) -> None:
        config, _ = make_config()
        env = worker_environment(config, make_claim())
        self.assertEqual(env["CURSOR_API_KEY"], "sa_dummy_cursor_key")
        self.assertEqual(env["CURSOR_AGENT_WORKER_ID"], "worker-abc123")
        self.assertEqual(env["CURSOR_WORKER_NAME"], "tl-worker")
        for banned in ("CURSOR_API_URL", "CURSOR_API_ENDPOINT", "CURSOR_REQUEST_ID", "CURSOR_POOL", "CURSOR_REPO_URL", "TENSORLAKE_API_KEY"):
            self.assertNotIn(banned, env, banned)


class PoolModeTests(unittest.TestCase):
    def test_default_is_repo_mode(self) -> None:
        config, _ = make_config()
        self.assertFalse(config.cursor_pool_any_repo)
        config, _ = make_config(CURSOR_POOL_MODE=" Repo ")
        self.assertFalse(config.cursor_pool_any_repo)

    def test_any_repo_mode(self) -> None:
        config, _ = make_config(CURSOR_POOL_MODE="any-repo")
        self.assertTrue(config.cursor_pool_any_repo)
        self.assertIsNone(config.cursor_pool_repo_url)

    def test_unknown_mode_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            make_config(CURSOR_POOL_MODE="multi")

    def test_any_repo_excludes_repo_pin(self) -> None:
        with self.assertRaises(ConfigError):
            make_config(CURSOR_POOL_MODE="any-repo", CURSOR_POOL_REPO_URL="https://github.com/acme/widgets")

    def test_clone_repos_replaces_checkout_hook(self) -> None:
        config, _ = make_config(WORKER_LABELS_JSON='{"team":"x"}')
        argv = worker_command(config, make_claim(), has_repo=False, clone_repos=True)
        start = argv.index("start")
        self.assertIn("--clone-git-repos", argv[:start])
        self.assertNotIn("--mint-github-token", argv)
        self.assertNotIn("--on-session-start", argv)
        self.assertNotIn(CHECKOUT_HOOK_PATH, argv)
        self.assertIn("--labels-file", argv)
        # clone wins even when the claim names a repository
        argv = worker_command(config, make_claim(), has_repo=True, clone_repos=True)
        self.assertIn("--clone-git-repos", argv)
        self.assertNotIn("--on-session-start", argv)
