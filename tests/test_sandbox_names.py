from __future__ import annotations

import unittest

from cursor_tensorlake.sandbox import (
    my_machine_sandbox_name,
    orchestrator_sandbox_name,
    sandbox_name_for,
)


class NameTests(unittest.TestCase):
    def test_slug_ids_map_directly(self) -> None:
        self.assertEqual(sandbox_name_for("worker-abc123"), "cursor-worker-abc123")
        self.assertEqual(sandbox_name_for("pw123"), "cursor-pw123")

    def test_unsafe_ids_are_hashed(self) -> None:
        name = sandbox_name_for("pw_123/ABC")
        self.assertTrue(name.startswith("cursor-pw-123-abc-"))
        self.assertLessEqual(len(name), 63)
        self.assertEqual(name, sandbox_name_for("pw_123/ABC"))
        self.assertNotEqual(name, sandbox_name_for("pw_123/ABD"))

    def test_long_ids_fit(self) -> None:
        name = sandbox_name_for("w" * 100)
        self.assertLessEqual(len(name), 63)

    def test_orchestrator_and_machine_names(self) -> None:
        self.assertTrue(orchestrator_sandbox_name("tensorlake").startswith("cursor-tl-orch-"))
        self.assertEqual(my_machine_sandbox_name("My Box!"), "cursor-tl-mm-my-box")
