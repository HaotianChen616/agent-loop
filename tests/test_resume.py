from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_loop.agent import ScriptedAgent
from agent_loop.config import load_run_spec
from agent_loop.engine import LoopEngine
from agent_loop.storage import StateStore
from agent_loop.types import ConfigError, RunStatus


SCENARIO = Path(__file__).parents[1] / "scenarios" / "approval-loop" / "scenario.toml"


class ResumeTests(unittest.TestCase):
    def make_engine(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        spec = load_run_spec(SCENARIO)
        store = StateStore(Path(temporary.name) / "runs")
        agent = ScriptedAgent.from_file(spec.agent.script or "")
        return LoopEngine(spec, agent, store), store

    def test_approval_executes_saved_action_once_then_completes(self) -> None:
        engine, store = self.make_engine()
        paused = engine.start("approval-run")

        self.assertEqual(paused.status, RunStatus.NEEDS_REVIEW)
        action_id = paused.pending_approval["action_id"]
        completed = engine.resume("approval-run", approval=True)
        events = store.read_events("approval-run")

        self.assertEqual(completed.status, RunStatus.COMPLETED)
        starts = [event for event in events if event["event_type"] == "tool_started"]
        self.assertEqual(sum(action_id in event["summary"] for event in starts), 1)
        self.assertEqual(completed.budget_usage.tool_calls, 2)

    def test_rejection_cancels_without_action(self) -> None:
        engine, _ = self.make_engine()
        engine.start("reject-run")
        state = engine.resume("reject-run", approval=False)
        self.assertEqual(state.status, RunStatus.CANCELLED)
        self.assertEqual(state.budget_usage.tool_calls, 0)

    def test_workspace_change_invalidates_approval(self) -> None:
        engine, store = self.make_engine()
        engine.start("changed-run")
        workspace_file = store.run_dir("changed-run") / "workspace" / "answer.txt"
        workspace_file.write_text("tampered\n")

        state = engine.resume("changed-run", approval=True)
        self.assertEqual(state.status, RunStatus.FAILED)

    def test_resume_rejects_a_different_agent(self) -> None:
        engine, store = self.make_engine()
        engine.start("agent-change-run")
        wrong_agent = ScriptedAgent([])
        wrong_agent.name = "different"

        with self.assertRaisesRegex(ConfigError, "original Agent"):
            LoopEngine(engine.spec, wrong_agent, store).resume("agent-change-run")

    def test_resume_rejects_a_different_provider(self) -> None:
        engine, store = self.make_engine()
        engine.agent.name = "llm"
        engine.agent.provider_name = "openai"
        engine.agent.model = "same-model"
        engine.start("provider-change-run")

        wrong_agent = ScriptedAgent.from_file(engine.spec.agent.script or "")
        wrong_agent.name = "llm"
        wrong_agent.provider_name = "zhipu-coding-plan"
        wrong_agent.model = "same-model"
        with self.assertRaisesRegex(ConfigError, "provider"):
            LoopEngine(engine.spec, wrong_agent, store).resume("provider-change-run")


if __name__ == "__main__":
    unittest.main()
