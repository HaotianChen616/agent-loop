from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_loop.agent import ScriptedAgent
from agent_loop.config import load_run_spec
from agent_loop.context import ContextBuilder
from agent_loop.tools import ToolRegistry
from agent_loop.types import AgentDecision, ConfigError, DecisionKind, RunState
from agent_loop.workspace import Workspace
from tests.test_config_workspace import ScenarioFixture


class ContextAgentTests(unittest.TestCase):
    def test_context_keeps_recent_evidence_and_script_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = ScenarioFixture(root / "scenario")
            spec = load_run_spec(fixture.path)
            workspace = Workspace.create(spec.workspace, root / "workspace")
            state = RunState(1, "run-1", spec.scenario_id, spec.digest)
            state.last_verification = {"verdict": "fail", "feedback": "answer missing"}
            context = ContextBuilder(spec, ToolRegistry(workspace)).build(state)
            agent = ScriptedAgent(
                [AgentDecision(DecisionKind.REQUEST_VERIFICATION, "check the result")]
            )

            self.assertIn("answer missing", context.prompt)
            self.assertEqual(agent.next_action(context).kind, DecisionKind.REQUEST_VERIFICATION)
            self.assertEqual(agent.next_action(context).kind, DecisionKind.BLOCKED)

    def test_script_file_is_strictly_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "actions.json"
            path.write_text(json.dumps([{"kind": "unknown", "summary": "bad"}]))
            with self.assertRaises(ConfigError):
                ScriptedAgent.from_file(path)


if __name__ == "__main__":
    unittest.main()
