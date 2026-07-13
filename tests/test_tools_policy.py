from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_loop.config import load_run_spec
from agent_loop.policy import PolicyEngine, StopPolicy
from agent_loop.tools import ToolRegistry
from agent_loop.types import RiskLevel, RunState, RunStatus, ToolStatus, Verdict, VerificationReport
from agent_loop.workspace import Workspace
from tests.test_config_workspace import ScenarioFixture


class ToolAndPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        fixture = ScenarioFixture(root / "scenario")
        self.spec = load_run_spec(fixture.path)
        workspace = Workspace.create(self.spec.workspace, root / "workspace")
        self.tools = ToolRegistry(workspace, max_output_chars=4)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_tools_return_bounded_observations(self) -> None:
        write = self.tools.execute("a1", "write_file", {"path": "answer.txt", "content": "abcdef"})
        read = self.tools.execute("a2", "read_file", {"path": "answer.txt"})

        self.assertEqual(write.status, ToolStatus.SUCCESS)
        self.assertEqual(read.output, "abcd")
        self.assertTrue(read.output_truncated)

    def test_unknown_or_unsafe_calls_become_errors(self) -> None:
        unknown = self.tools.execute("a1", "shell", {})
        protected = self.tools.execute(
            "a2", "write_file", {"path": "requirements.txt", "content": "changed"}
        )

        self.assertEqual(unknown.status, ToolStatus.ERROR)
        self.assertEqual(protected.status, ToolStatus.ERROR)

    def test_policy_and_stop_decisions(self) -> None:
        policy = PolicyEngine()
        self.assertTrue(policy.authorize(RiskLevel.READ, self.spec).allowed)
        self.assertTrue(policy.authorize(RiskLevel.EXTERNAL_WRITE, self.spec).needs_approval)
        self.assertTrue(policy.authorize(RiskLevel.IRREVERSIBLE, self.spec).denied)

        state = RunState(1, "run-1", self.spec.scenario_id, self.spec.digest)
        report = VerificationReport(Verdict.PASS, (), "done")
        decision = StopPolicy().after_verification(report, 0, self.spec)
        self.assertEqual(decision.status, RunStatus.COMPLETED)


if __name__ == "__main__":
    unittest.main()
