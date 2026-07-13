from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from agent_loop.agent import ScriptedAgent
from agent_loop.config import load_run_spec
from agent_loop.engine import LoopEngine
from agent_loop.storage import StateStore
from agent_loop.types import AgentDecision, DecisionKind, RunStatus


SCENARIO = Path(__file__).parents[1] / "scenarios" / "hello-loop"


class EngineTests(unittest.TestCase):
    def run_with(self, agent: ScriptedAgent, scenario: Path = SCENARIO, **engine_options):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        spec = load_run_spec(scenario / "scenario.toml")
        store = StateStore(Path(temporary.name) / "runs")
        state = LoopEngine(spec, agent, store, **engine_options).start("engine-run")
        return state, store

    def test_feedback_path_completes_with_verifier_evidence(self) -> None:
        spec = load_run_spec(SCENARIO / "scenario.toml")
        state, store = self.run_with(ScriptedAgent.from_file(spec.agent.script or ""))

        self.assertEqual(state.status, RunStatus.COMPLETED)
        self.assertEqual(state.iteration, 2)
        self.assertEqual(state.budget_usage.verifications, 3)
        self.assertEqual(state.last_verification["verdict"], "pass")
        events = (store.run_dir(state.run_id) / "events.jsonl").read_text()
        self.assertIn('"event_type": "action_proposed"', events)
        self.assertIn('"event_type": "verification_completed"', events)

    def test_initially_satisfied_workspace_skips_agent(self) -> None:
        with tempfile.TemporaryDirectory() as scenario_dir:
            copied = Path(scenario_dir) / "scenario"
            shutil.copytree(SCENARIO, copied)
            (copied / "fixture" / "implementation.txt").write_text("Hello, loop!\n")
            agent = ScriptedAgent([])
            state, _ = self.run_with(agent, copied)

            self.assertEqual(state.status, RunStatus.COMPLETED)
            self.assertEqual(agent.calls, 0)

    def test_repeated_failure_stops_without_infinite_retry(self) -> None:
        wrong = AgentDecision(
            DecisionKind.TOOL_CALL,
            "write the same wrong answer",
            "write_file",
            {"path": "implementation.txt", "content": "wrong\n"},
        )
        state, _ = self.run_with(ScriptedAgent([wrong, wrong]))

        self.assertEqual(state.status, RunStatus.FAILED)
        self.assertEqual(state.stop_reason, "same verification failure repeated")

    def test_agent_claim_cannot_bypass_verifier(self) -> None:
        agent = ScriptedAgent(
            [AgentDecision(DecisionKind.REQUEST_VERIFICATION, "I think this is complete")]
        )
        state, _ = self.run_with(agent)
        self.assertNotEqual(state.status, RunStatus.COMPLETED)

    def test_elapsed_budget_is_rechecked_after_agent_call(self) -> None:
        correct = AgentDecision(
            DecisionKind.TOOL_CALL,
            "write a correct answer",
            "write_file",
            {"path": "implementation.txt", "content": "Hello, loop!\n"},
        )
        clock = iter([0.0, 0.1, 0.2, 0.3, 31.0]).__next__
        state, _ = self.run_with(ScriptedAgent([correct]), clock=clock)

        self.assertEqual(state.status, RunStatus.BUDGET_EXHAUSTED)
        self.assertEqual(state.budget_usage.tool_calls, 0)

    def test_invalid_adapter_output_cannot_support_blocked_claim(self) -> None:
        invalid = AgentDecision(
            DecisionKind.TOOL_CALL,
            "invalid arguments",
            "write_file",
            {"path": "answer.txt", "content": object()},
        )
        blocked = AgentDecision(DecisionKind.BLOCKED, "cannot continue")
        state, _ = self.run_with(ScriptedAgent([invalid, blocked]))

        self.assertEqual(state.status, RunStatus.NEEDS_REVIEW)
        self.assertEqual(state.last_tool_result["tool_name"], "agent")

    def test_fresh_missing_file_error_can_support_blocked_claim(self) -> None:
        missing = AgentDecision(
            DecisionKind.TOOL_CALL,
            "read required input",
            "read_file",
            {"path": "missing.txt"},
        )
        blocked = AgentDecision(DecisionKind.BLOCKED, "required input is missing")
        state, _ = self.run_with(ScriptedAgent([missing, blocked]))

        self.assertEqual(state.status, RunStatus.BLOCKED)


if __name__ == "__main__":
    unittest.main()
