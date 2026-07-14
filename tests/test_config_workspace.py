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
        root.mkdir(parents=True, exist_ok=True)
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

    def test_rejects_scenario_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = ScenarioFixture(root / "scenario")
            (root / "outside.py").write_text("print('{}')")
            fixture.path.write_text(SCENARIO.replace('"checks/verify.py"', '"../outside.py"'))
            with self.assertRaisesRegex(ConfigError, "inside"):
                load_run_spec(fixture.path)

    def test_digest_includes_referenced_scenario_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ScenarioFixture(Path(directory))
            helper = fixture.root / "checks" / "helper.py"
            helper.write_text("EXPECTED = 'first'\n")
            before = load_run_spec(fixture.path).digest
            helper.write_text("EXPECTED = 'second'\n")

            self.assertNotEqual(load_run_spec(fixture.path).digest, before)

    def test_loads_a_zhipu_coding_plan_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ScenarioFixture(Path(directory))
            fixture.path.write_text(
                SCENARIO
                + '\n\n[agent]\nkind = "llm"\nprovider = "zhipu-coding-plan"'
                + '\nmodel = "GLM-5.1"\n'
            )

            spec = load_run_spec(fixture.path)

            self.assertEqual(spec.agent.kind, "llm")
            self.assertEqual(spec.agent.provider, "zhipu-coding-plan")
            self.assertEqual(spec.agent.model, "GLM-5.1")

    def test_rejects_unknown_or_scripted_providers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ScenarioFixture(Path(directory))
            fixture.path.write_text(
                SCENARIO + '\n\n[agent]\nkind = "llm"\nprovider = "unknown"\n'
            )
            with self.assertRaisesRegex(ConfigError, "unknown agent.provider"):
                load_run_spec(fixture.path)

            fixture.path.write_text(
                SCENARIO + '\n\n[agent]\nkind = "scripted"\nprovider = "openai"\n'
            )
            with self.assertRaisesRegex(ConfigError, "only be used"):
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

    def test_digest_rejects_internal_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.make_workspace(directory)
            (workspace.root / "alias.txt").symlink_to(
                workspace.root / "requirements.txt"
            )

            with self.assertRaisesRegex(PathViolation, "cannot digest symlink"):
                workspace.digest()

    def test_runs_do_not_share_mutable_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = ScenarioFixture(Path(directory) / "scenario")
            spec = load_run_spec(fixture.path)
            first = Workspace.create(spec.workspace, Path(directory) / "run-a" / "workspace")
            second = Workspace.create(spec.workspace, Path(directory) / "run-b" / "workspace")

            first.write_text("answer.txt", "only run A\n")

            self.assertFalse((second.root / "answer.txt").exists())
            self.assertNotEqual(first.digest(), second.digest())


if __name__ == "__main__":
    unittest.main()
