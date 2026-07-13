from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from agent_loop.cli import main


ROOT = Path(__file__).parents[1]
HELLO = ROOT / "scenarios" / "hello-loop" / "scenario.toml"
APPROVAL = ROOT / "scenarios" / "approval-loop" / "scenario.toml"


class CliTests(unittest.TestCase):
    def test_run_and_inspect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(
                    ["run", str(HELLO), "--run-id", "cli-run", "--runs-dir", directory]
                )
                inspect_code = main(["inspect", "cli-run", "--runs-dir", directory])

            self.assertEqual(code, 0)
            self.assertEqual(inspect_code, 0)
            self.assertIn("status=completed", output.getvalue())
            self.assertIn("VERIFICATION COMPLETED", output.getvalue())

    def test_approval_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with redirect_stdout(io.StringIO()):
                paused = main(
                    ["run", str(APPROVAL), "--run-id", "cli-approval", "--runs-dir", directory]
                )
                completed = main(
                    ["resume", "cli-approval", "--approve", "--runs-dir", directory]
                )

            self.assertEqual(paused, 2)
            self.assertEqual(completed, 0)


if __name__ == "__main__":
    unittest.main()
