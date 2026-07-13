from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_loop.config import load_run_spec
from agent_loop.types import ConfigError, PathViolation
from agent_loop.workspace import Workspace


SCENARIO = """
schema_version = 1
scenario_id = "test-loop"
title = "Test loop"
learning_objective = "Exercise the control loop"
goal = "Write an answer"
acceptance_criteria = ["answer-present"]
instructions = ["skill.md"]
allowed_tools = ["list_files", "read_file", "write_file"]

[workspace]
seed = "fixture"
read_only = ["requirements.txt"]

[verification]
script = "checks/verify.py"

[budget]
max_iterations = 2
max_agent_calls = 2
max_tool_calls = 3
max_verifications = 3
max_elapsed_seconds = 30
max_same_failure = 1
""".strip()


class ScenarioFixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "fixture").mkdir()
        (root / "checks").mkdir()
        (root / "fixture" / "requirements.txt").write_text("answer required\n")
        (root / "skill.md").write_text("Follow the requirements.\n")
        (root / "checks" / "verify.py").write_text("print('{}')\n")
        self.path = root / "scenario.toml"
        self.path.write_text(SCENARIO)


class ConfigTests(unittest.TestCase):
    def test_loads_and_resolves_scenario(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ScenarioFixture(Path(directory))
            spec = load_run_spec(fixture.path)

            self.assertEqual(spec.scenario_id, "test-loop")
            self.assertTrue(Path(spec.workspace.seed).is_absolute())
            self.assertTrue(Path(spec.verification.script).is_file())
            self.assertEqual(len(spec.digest), 64)

    def test_rejects_duplicate_criteria(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ScenarioFixture(Path(directory))
            fixture.path.write_text(
                SCENARIO.replace('["answer-present"]', '["answer-present", "answer-present"]')
            )
            with self.assertRaisesRegex(ConfigError, "unique"):
                load_run_spec(fixture.path)


class WorkspaceTests(unittest.TestCase):
    def make_workspace(self, directory: str) -> Workspace:
        fixture = ScenarioFixture(Path(directory))
        spec = load_run_spec(fixture.path)
        return Workspace.create(spec.workspace, Path(directory) / "run" / "workspace")

    def test_read_write_and_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.make_workspace(directory)
            before = workspace.digest()
            workspace.write_text("answer.txt", "hello\n")

            self.assertEqual(workspace.read_text("answer.txt"), "hello\n")
            self.assertIn("answer.txt", workspace.list_files())
            self.assertNotEqual(before, workspace.digest())

    def test_rejects_traversal_and_read_only_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.make_workspace(directory)
            with self.assertRaises(PathViolation):
                workspace.read_text("../outside.txt")
            with self.assertRaisesRegex(PathViolation, "read-only"):
                workspace.write_text("requirements.txt", "changed")

    def test_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.make_workspace(directory)
            outside = Path(directory) / "outside.txt"
            outside.write_text("secret")
            (workspace.root / "escape.txt").symlink_to(outside)

            with self.assertRaisesRegex(PathViolation, "escapes"):
                workspace.read_text("escape.txt")


if __name__ == "__main__":
    unittest.main()
