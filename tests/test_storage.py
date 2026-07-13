from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agent_loop.config import load_run_spec
from agent_loop.storage import StateStore, jsonable
from agent_loop.types import RunStatus
from tests.test_config_workspace import ScenarioFixture


class StateStoreTests(unittest.TestCase):
    def test_checkpoint_round_trip_and_event_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = ScenarioFixture(root / "scenario")
            spec = load_run_spec(fixture.path)
            store = StateStore(root / "runs")
            state = store.create(spec, "run-1")

            state.status = RunStatus.RUNNING
            store.checkpoint(state, "state_transitioned", "created -> running")
            loaded = store.load("run-1")
            events = [
                json.loads(line)
                for line in (store.run_dir("run-1") / "events.jsonl").read_text().splitlines()
            ]

            self.assertEqual(loaded.status, RunStatus.RUNNING)
            self.assertEqual(loaded.revision, 2)
            self.assertEqual([event["sequence"] for event in events], [1, 2])
            self.assertEqual(events[-1]["state_revision"], loaded.revision)

    def test_manifest_freezes_scenario_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = ScenarioFixture(root / "scenario")
            spec = load_run_spec(fixture.path)
            store = StateStore(root / "runs")
            store.create(spec, "run-2")
            manifest = json.loads((store.run_dir("run-2") / "manifest.json").read_text())

            self.assertEqual(manifest["scenario"]["digest"], spec.digest)
            self.assertNotEqual(replace(spec, digest="changed").digest, spec.digest)

    def test_rejects_unsafe_artifact_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "runs")
            with self.assertRaises(ValueError):
                store.write_artifact("run-3", "../escape", "bad")

    def test_recovery_repairs_state_ahead_and_partial_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = ScenarioFixture(root / "scenario")
            spec = load_run_spec(fixture.path)
            store = StateStore(root / "runs")
            state = store.create(spec, "run-4")
            state.revision += 1  # Simulate a crash after state replace but before event append.
            StateStore._atomic_json(store.run_dir("run-4") / "state.json", jsonable(state))
            with (store.run_dir("run-4") / "events.jsonl").open("a") as handle:
                handle.write('{"partial":')

            recovered = store.recover("run-4")
            events = store.read_events("run-4")
            self.assertGreater(recovered.revision, state.revision)
            self.assertEqual(events[-1]["event_type"], "recovery_performed")

    def test_event_ahead_pauses_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = ScenarioFixture(root / "scenario")
            spec = load_run_spec(fixture.path)
            store = StateStore(root / "runs")
            state = store.create(spec, "run-5")
            event = store.read_events("run-5")[-1]
            event["state_revision"] = state.revision + 5
            event["sequence"] = state.event_sequence + 5
            with (store.run_dir("run-5") / "events.jsonl").open("a") as handle:
                handle.write(json.dumps(event) + "\n")

            recovered = store.recover("run-5")
            self.assertEqual(recovered.status, RunStatus.NEEDS_REVIEW)


if __name__ == "__main__":
    unittest.main()
