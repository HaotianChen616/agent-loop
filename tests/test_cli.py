from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_loop.cli import _agent, main
from agent_loop.config import load_run_spec


ROOT = Path(__file__).parents[1]
HELLO = ROOT / "scenarios" / "hello-loop" / "scenario.toml"
HAPPY = ROOT / "scenarios" / "hello-loop" / "happy.toml"
BUDGET = ROOT / "scenarios" / "hello-loop" / "budget.toml"
APPROVAL = ROOT / "scenarios" / "approval-loop" / "scenario.toml"


class CliTests(unittest.TestCase):
    def test_builds_the_selected_maas_provider(self) -> None:
        spec = load_run_spec(HELLO)
        provider = SimpleNamespace(name="zhipu-coding-plan", model="glm-5.1")
        with patch("agent_loop.cli.create_provider", return_value=provider) as factory:
            agent = _agent(spec, "llm", "glm-5.1", "zhipu-coding-plan")

        self.assertEqual(agent.provider_name, "zhipu-coding-plan")
        factory.assert_called_once_with(
            "zhipu-coding-plan",
            "glm-5.1",
            spec.agent.request_timeout_seconds,
            spec.agent.max_output_tokens,
        )

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

    def test_happy_and_budget_teaching_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with redirect_stdout(io.StringIO()):
                happy = main(
                    ["run", str(HAPPY), "--run-id", "happy-resume", "--runs-dir", directory]
                )
                resumed = main(["resume", "happy-resume", "--runs-dir", directory])
                budget = main(["run", str(BUDGET), "--runs-dir", directory])

            self.assertEqual(happy, 0)
            self.assertEqual(resumed, 0)
            self.assertEqual(budget, 1)

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

    def test_apply_requires_an_explicit_yes_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            target = root / "target"
            target.mkdir()
            with redirect_stdout(io.StringIO()):
                run_code = main(
                    ["run", str(HELLO), "--run-id", "cli-apply", "--runs-dir", str(runs)]
                )
                apply_code = main(
                    ["apply", "cli-apply", str(target), "--runs-dir", str(runs), "--yes"]
                )

            self.assertEqual(run_code, 0)
            self.assertEqual(apply_code, 0)
            self.assertEqual((target / "implementation.txt").read_text(), "Hello, loop!\n")


if __name__ == "__main__":
    unittest.main()
