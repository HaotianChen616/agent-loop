"""Deterministic verification on a disposable workspace snapshot."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .storage import StateStore
from .types import CriterionResult, RunSpec, Verdict, VerificationReport
from .workspace import Workspace


class PythonScriptVerifier:
    """Run a trusted scenario check without granting it the Agent workspace."""

    def __init__(self, spec: RunSpec, workspace: Workspace, store: StateStore) -> None:
        self.spec = spec
        self.workspace = workspace
        self.store = store

    def verify(self, run_id: str) -> VerificationReport:
        started = time.monotonic()
        verification_id = uuid.uuid4().hex
        run_dir = self.store.run_dir(run_id)
        with tempfile.TemporaryDirectory(prefix="verify-", dir=run_dir) as temporary:
            temporary_path = Path(temporary)
            snapshot = self.workspace.copy_snapshot(temporary_path / "workspace")
            environment = self._environment(temporary_path, snapshot)
            try:
                completed = subprocess.run(
                    [sys.executable, self.spec.verification.script],
                    cwd=snapshot,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=self.spec.verification.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout or ""
                stderr = exc.stderr or ""
                refs = self._write_evidence(run_id, verification_id, stdout, stderr, "timeout")
                return self._inconclusive("verification timed out", refs, started)

            refs = self._write_evidence(
                run_id, verification_id, completed.stdout, completed.stderr, str(completed.returncode)
            )
            return self._parse(completed.stdout, completed.returncode, refs, started)

    @staticmethod
    def _environment(temporary: Path, snapshot: Path) -> dict[str, str]:
        # Preserve only platform essentials; credentials and application env do
        # not cross the verifier boundary.
        environment = {
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": str(temporary),
            "AGENT_LOOP_WORKSPACE": str(snapshot),
        }
        for key in ("PATH", "SYSTEMROOT"):
            if key in os.environ:
                environment[key] = os.environ[key]
        return environment

    def _parse(
        self,
        stdout: str,
        returncode: int,
        refs: tuple[str, ...],
        started: float,
    ) -> VerificationReport:
        if len(stdout) > 1_000_000:
            return self._inconclusive("verification output exceeded limit", refs, started)
        try:
            payload = json.loads(stdout)
            results = self._criterion_results(payload)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            return self._inconclusive(f"invalid verification protocol: {exc}", refs, started)

        verdicts = {result.verdict for result in results}
        overall = (
            Verdict.INCONCLUSIVE
            if Verdict.INCONCLUSIVE in verdicts
            else Verdict.FAIL
            if Verdict.FAIL in verdicts
            else Verdict.PASS
        )
        expected_code = {Verdict.PASS: 0, Verdict.FAIL: 1, Verdict.INCONCLUSIVE: 2}[overall]
        if returncode != expected_code:
            return self._inconclusive(
                f"exit code {returncode} conflicts with {overall.value} results", refs, started
            )

        failed = [item for item in results if item.verdict is not Verdict.PASS]
        feedback = "all acceptance criteria passed" if not failed else "; ".join(
            f"{item.criterion_id}: {item.message}" for item in failed
        )
        fingerprint = None
        if failed:
            normalized = "\n".join(
                f"{item.criterion_id}:{item.verdict.value}:{item.message.strip()}" for item in failed
            )
            fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return VerificationReport(
            overall,
            results,
            feedback,
            refs,
            fingerprint,
            overall is Verdict.FAIL,
            int((time.monotonic() - started) * 1_000),
        )

    def _criterion_results(self, payload: Any) -> tuple[CriterionResult, ...]:
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("schema_version must be 1")
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise ValueError("results must be a list")
        results = []
        for item in raw_results:
            if not isinstance(item, dict):
                raise ValueError("each result must be an object")
            evidence = item.get("evidence", [])
            if not isinstance(evidence, list) or not all(isinstance(value, str) for value in evidence):
                raise ValueError("evidence must be a string list")
            results.append(
                CriterionResult(
                    str(item["criterion_id"]),
                    Verdict(item["verdict"]),
                    str(item["message"]),
                    tuple(evidence),
                )
            )
        ids = [result.criterion_id for result in results]
        if len(ids) != len(set(ids)) or set(ids) != set(self.spec.acceptance_criteria):
            raise ValueError("criterion IDs must exactly match the scenario")
        return tuple(results)

    def _write_evidence(
        self, run_id: str, verification_id: str, stdout: str, stderr: str, exit_code: str
    ) -> tuple[str, ...]:
        base = f"verification/{verification_id}"
        return (
            self.store.write_artifact(run_id, f"{base}/stdout.txt", stdout[:1_000_000]),
            self.store.write_artifact(run_id, f"{base}/stderr.txt", stderr[:1_000_000]),
            self.store.write_artifact(run_id, f"{base}/exit_code.txt", exit_code),
        )

    @staticmethod
    def _inconclusive(
        feedback: str, refs: tuple[str, ...], started: float
    ) -> VerificationReport:
        return VerificationReport(
            Verdict.INCONCLUSIVE,
            (),
            feedback,
            refs,
            retryable=False,
            duration_ms=int((time.monotonic() - started) * 1_000),
        )
