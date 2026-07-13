from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_loop.agent import ScriptedAgent
from agent_loop.application import apply_run
from agent_loop.config import load_run_spec
from agent_loop.engine import LoopEngine
from agent_loop.storage import StateStore
from agent_loop.types import ApplyError


SCENARIO = Path(__file__).parents[1] / "scenarios" / "hello-loop" / "scenario.toml"


class ApplicationTests(unittest.TestCase):
    def completed_run(self, root: Path) -> tuple[StateStore, str]:
        spec = load_run_spec(SCENARIO)
        store = StateStore(root / "runs")
        agent = ScriptedAgent.from_file(spec.agent.script or "")
        state = LoopEngine(spec, agent, store).start("apply-run")
        return store, state.run_id

    def test_confirmed_apply_is_audited_without_changing_run_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, run_id = self.completed_run(root)
            target = root / "target"
            target.mkdir()
            state_path = store.run_dir(run_id) / "state.json"
            events_path = store.run_dir(run_id) / "events.jsonl"
            state_before, events_before = state_path.read_bytes(), events_path.read_bytes()

            record = apply_run(store, run_id, target, lambda preview: True)

            self.assertEqual(record["status"], "applied")
            self.assertEqual((target / "implementation.txt").read_text(), "Hello, loop!\n")
            self.assertEqual(state_path.read_bytes(), state_before)
            self.assertEqual(events_path.read_bytes(), events_before)
            audit = store.run_dir(run_id) / "applications" / f"{record['application_id']}.json"
            self.assertEqual(json.loads(audit.read_text())["confirmed"], True)

    def test_declined_apply_has_no_target_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, run_id = self.completed_run(root)
            target = root / "target"
            target.mkdir()

            record = apply_run(store, run_id, target, lambda preview: False)

            self.assertEqual(record["status"], "declined")
            self.assertEqual(tuple(target.iterdir()), ())

    def test_workspace_change_during_confirmation_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, run_id = self.completed_run(root)
            target = root / "target"
            target.mkdir()

            def mutate_then_confirm(preview) -> bool:
                workspace = store.run_dir(run_id) / "workspace" / "implementation.txt"
                workspace.write_text("changed after verification\n")
                return True

            with self.assertRaisesRegex(ApplyError, "during confirmation"):
                apply_run(store, run_id, target, mutate_then_confirm)
            self.assertEqual(tuple(target.iterdir()), ())
            audits = tuple((store.run_dir(run_id) / "applications").glob("*.json"))
            self.assertEqual(json.loads(audits[0].read_text())["status"], "failed")

    def test_non_completed_run_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = load_run_spec(SCENARIO)
            store = StateStore(root / "runs")
            state = store.create(spec, "unfinished-run")
            target = root / "target"
            target.mkdir()

            with self.assertRaisesRegex(ApplyError, "completed"):
                apply_run(store, state.run_id, target, lambda preview: True)

    def test_target_symlink_is_never_followed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, run_id = self.completed_run(root)
            target = root / "target"
            target.mkdir()
            outside = root / "outside.txt"
            outside.write_text("outside stays unchanged\n")
            (target / "implementation.txt").symlink_to(outside)

            with self.assertRaisesRegex(ApplyError, "safe regular file"):
                apply_run(store, run_id, target, lambda preview: True)
            self.assertEqual(outside.read_text(), "outside stays unchanged\n")


if __name__ == "__main__":
    unittest.main()
