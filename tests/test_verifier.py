from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_loop.config import load_run_spec
from agent_loop.storage import StateStore
from agent_loop.types import Verdict
from agent_loop.verifier import PythonScriptVerifier
from agent_loop.workspace import Workspace
from tests.test_config_workspace import ScenarioFixture


class VerifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.fixture = ScenarioFixture(root / "scenario")
        self.store = StateStore(root / "runs")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def verify_with(self, payload: dict, returncode: int) -> tuple[Verdict, Workspace]:
        script = self.fixture.root / "checks" / "verify.py"
        script.write_text(
            "import json, pathlib, sys\n"
            "pathlib.Path('verifier-cache.txt').write_text('temporary')\n"
            f"print(json.dumps({payload!r}))\n"
            f"sys.exit({returncode})\n"
        )
        spec = load_run_spec(self.fixture.path)
        self.store.create(spec, "verify-run")
        workspace = Workspace.create(spec.workspace, self.store.run_dir("verify-run") / "workspace")
        report = PythonScriptVerifier(spec, workspace, self.store).verify("verify-run")
        return report.verdict, workspace

    def test_passes_exact_criteria_without_mutating_workspace(self) -> None:
        verdict, workspace = self.verify_with(
            {
                "schema_version": 1,
                "results": [
                    {
                        "criterion_id": "answer-present",
                        "verdict": "pass",
                        "message": "answer exists",
                        "evidence": ["answer.txt"],
                    }
                ],
            },
            0,
        )

        self.assertEqual(verdict, Verdict.PASS)
        self.assertNotIn("verifier-cache.txt", workspace.list_files())

    def test_missing_criterion_is_inconclusive(self) -> None:
        verdict, _ = self.verify_with({"schema_version": 1, "results": []}, 0)
        self.assertEqual(verdict, Verdict.INCONCLUSIVE)

    def test_exit_code_mismatch_is_inconclusive(self) -> None:
        verdict, _ = self.verify_with(
            {
                "schema_version": 1,
                "results": [
                    {
                        "criterion_id": "answer-present",
                        "verdict": "fail",
                        "message": "missing",
                    }
                ],
            },
            0,
        )
        self.assertEqual(verdict, Verdict.INCONCLUSIVE)


if __name__ == "__main__":
    unittest.main()
