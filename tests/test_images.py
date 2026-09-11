"""Image recipes: the computer-use variant and the orchestrator package source."""

from __future__ import annotations

import os
import unittest

from cursor_tensorlake import orchestrator_image, worker_image


class WorkerRecipeTests(unittest.TestCase):
    def test_computer_use_adds_desktop_step_and_changes_name(self) -> None:
        plain = worker_image.build_recipe("tensorlake/ubuntu-minimal", "v", "sum", "x64")
        desktop = worker_image.build_recipe("tensorlake/ubuntu-vnc", "v", "sum", "x64", computer_use=True)
        self.assertNotIn(("run", worker_image.APT_DESKTOP), plain)
        self.assertIn(("run", worker_image.APT_DESKTOP), desktop)
        for pkg in ("xdotool", "ffmpeg", "tigervnc-standalone-server", "xfce4", "x11-utils"):
            self.assertIn(pkg, worker_image.APT_DESKTOP)
        self.assertNotEqual(worker_image.recipe_sha8(plain), worker_image.recipe_sha8(desktop))

    def test_default_base_follows_computer_use(self) -> None:
        os.environ.pop("IMAGE_BASE", None)
        self.assertEqual(worker_image.default_base_image(False), "tensorlake/ubuntu-minimal")
        self.assertEqual(worker_image.default_base_image(True), "tensorlake/ubuntu-vnc")
        os.environ["IMAGE_BASE"] = "ghcr.io/acme/base"
        try:
            self.assertEqual(worker_image.default_base_image(True), "ghcr.io/acme/base")
        finally:
            del os.environ["IMAGE_BASE"]

    def test_image_name_ignores_stale_recipe_pin(self) -> None:
        os.environ.pop("IMAGE_NAME", None)
        self.assertEqual(worker_image.image_name("a02105f0"), "cursor-tl-worker-a02105f0")
        cases = {
            "cursor-tl-worker-922bfa0e": "cursor-tl-worker-a02105f0",  # stale pin from another recipe
            "cursor-tl-worker-a02105f0": "cursor-tl-worker-a02105f0",  # current pin
            "acme/custom-worker": "acme/custom-worker",  # hand-built image keeps its name
        }
        for pinned, expected in cases.items():
            os.environ["IMAGE_NAME"] = pinned
            try:
                self.assertEqual(worker_image.image_name("a02105f0"), expected, pinned)
            finally:
                del os.environ["IMAGE_NAME"]

    def test_computer_use_env_flag(self) -> None:
        os.environ["WORKER_COMPUTER_USE"] = "true"
        try:
            self.assertTrue(worker_image.computer_use_enabled())
        finally:
            del os.environ["WORKER_COMPUTER_USE"]
        self.assertFalse(worker_image.computer_use_enabled())


    def test_no_recipe_step_spans_lines(self) -> None:
        # Each step becomes one RUN instruction. A newline inside one ends the
        # instruction early and the Dockerfile fails to parse.
        from cursor_tensorlake.worker_image import build_recipe
        from cursor_tensorlake.orchestrator_image import orchestrator_recipe

        recipes = [
            build_recipe("tensorlake/ubuntu-minimal", "v", "b", "x64"),
            build_recipe("tensorlake/ubuntu-vnc", "v", "b", "x64", computer_use=True),
            orchestrator_recipe("tensorlake/ubuntu-minimal", "v", "b", "x64"),
        ]
        for recipe in recipes:
            for kind, value in recipe:
                if kind in ("run", "base", "workdir"):
                    self.assertNotIn("\n", str(value), f"{kind} step spans lines: {value!r:.80}")

    def test_computer_use_clears_the_chrome_first_run_dialog(self) -> None:
        # Chrome's first-run terms dialog covers the whole screen, so an agent
        # told to look at a page sees the dialog instead.
        from cursor_tensorlake.worker_image import build_recipe

        steps = [
            str(value) for kind, value in
            build_recipe("tensorlake/ubuntu-vnc", "v", "b", "x64", computer_use=True)
            if kind == "run"
        ]
        self.assertTrue(any("First Run" in step for step in steps), steps)
        plain = [
            str(value) for kind, value in
            build_recipe("tensorlake/ubuntu-minimal", "v", "b", "x64") if kind == "run"
        ]
        self.assertFalse(any("First Run" in step for step in plain))


class OrchestratorRecipeTests(unittest.TestCase):
    def test_checkout_recipe_copies_package(self) -> None:
        recipe = orchestrator_image.orchestrator_recipe("tensorlake/ubuntu-minimal", "v", "sum", "x64")
        copies = [value for kind, value in recipe if kind == "copy"]
        self.assertIn(["pyproject.toml", f"{orchestrator_image.INSTALL_DIR}/pyproject.toml"], copies)
        self.assertTrue(any(str(v[0]).startswith("cursor_tensorlake/") for v in copies))

    def test_no_checkout_installs_from_source(self) -> None:
        os.environ["PACKAGE_SOURCE"] = "git+https://example.com/fork"
        try:
            recipe = orchestrator_image.orchestrator_recipe(
                "tensorlake/ubuntu-minimal", "v", "sum", "x64", from_checkout=False
            )
        finally:
            del os.environ["PACKAGE_SOURCE"]
        self.assertFalse([1 for kind, _ in recipe if kind == "copy"])
        runs = [str(value) for kind, value in recipe if kind == "run"]
        self.assertTrue(any("git+https://example.com/fork" in r for r in runs))

    def test_orchestrator_base_ignores_image_base(self) -> None:
        os.environ["IMAGE_BASE"] = "tensorlake/ubuntu-vnc"
        try:
            self.assertEqual(orchestrator_image.orchestrator_base_image(), "tensorlake/ubuntu-minimal")
        finally:
            del os.environ["IMAGE_BASE"]


if __name__ == "__main__":
    unittest.main()
