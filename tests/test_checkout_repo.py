from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


CHECKOUT_SCRIPT = Path(__file__).parents[1] / "cursor_tensorlake" / "checkout_repo.sh"


def _planner_source() -> str:
    shell = CHECKOUT_SCRIPT.read_text()
    start = "plan=\"$(python3 -c '\n"
    end = "\n' \"$workspace\" \"$repos_dir\")\""
    return shell.split(start, 1)[1].split(end, 1)[0]


class CheckoutPlannerTests(unittest.TestCase):
    def test_positional_refs_use_manifest_request_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "workspace"
            repos_dir = root / "repos"
            extra = repos_dir / "infra"
            workspace.mkdir()
            extra.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(extra)], check=True)
            subprocess.run(
                ["git", "-C", str(extra), "remote", "add", "origin", "https://github.com/acme/infra"],
                check=True,
            )
            # Claim position 1 was invalid and dropped. The remaining extra
            # root still belongs to payload position 2, not position 1.
            (repos_dir / ".roots").write_text(f"2\t{extra}\n")
            payload = {
                "repos": [
                    {"primary": True, "ref": "main"},
                    {"ref": "invalid-ref"},
                    {"ref": "infra-ref"},
                ]
            }

            result = subprocess.run(
                [sys.executable, "-c", _planner_source(), str(workspace), str(repos_dir)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                check=True,
            )

        self.assertEqual(
            result.stdout.splitlines(),
            [f"{workspace}\tmain", f"{extra}\tinfra-ref"],
        )


if __name__ == "__main__":
    unittest.main()
