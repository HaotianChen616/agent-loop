"""Durable run snapshots, append-only events, and artifacts."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, fields
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from .types import BudgetUsage, LoopEvent, RunSpec, RunState, RunStatus


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def jsonable(value: Any) -> Any:
    """Convert dataclasses and enums to stable JSON-compatible values."""

    if hasattr(value, "__dataclass_fields__"):
        return {key: jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    return value


class StateStore:
    """Use state.json as recovery truth and events.jsonl as its audit trail."""

    def __init__(self, root: str | Path = ".agent-loop/runs") -> None:
        self.root = Path(root).resolve()

    def run_dir(self, run_id: str) -> Path:
        if not run_id or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in run_id):
            raise ValueError("run_id must contain lowercase letters, digits, and hyphens")
        return self.root / run_id

    def create(self, spec: RunSpec, run_id: str | None = None) -> RunState:
        run_id = run_id or uuid.uuid4().hex
        directory = self.run_dir(run_id)
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "artifacts").mkdir()

        now = utc_now()
        state = RunState(
            schema_version=1,
            run_id=run_id,
            scenario_id=spec.scenario_id,
            scenario_digest=spec.digest,
            started_at=now,
            updated_at=now,
        )
        manifest = {
            "schema_version": 1,
            "created_at": now,
            "scenario": jsonable(spec),
        }
        self._atomic_json(directory / "manifest.json", manifest)
        self.checkpoint(state, "run_started", f"started scenario {spec.scenario_id}")
        return state

    def load(self, run_id: str) -> RunState:
        data = json.loads((self.run_dir(run_id) / "state.json").read_text(encoding="utf-8"))
        allowed = {item.name for item in fields(RunState)}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"state contains unknown fields: {sorted(unknown)}")
        data["status"] = RunStatus(data["status"])
        data["budget_usage"] = BudgetUsage(**data.get("budget_usage", {}))
        return RunState(**data)

    def checkpoint(
        self,
        state: RunState,
        event_type: str,
        summary: str,
        *,
        artifact_refs: tuple[str, ...] = (),
        duration_ms: int = 0,
        usage: dict[str, Any] | None = None,
    ) -> LoopEvent:
        """Commit state first, then append an event referencing that revision."""

        state.revision += 1
        state.event_sequence += 1
        state.updated_at = utc_now()
        directory = self.run_dir(state.run_id)
        self._atomic_json(directory / "state.json", jsonable(state))

        event = LoopEvent(
            event_id=uuid.uuid4().hex,
            sequence=state.event_sequence,
            state_revision=state.revision,
            run_id=state.run_id,
            iteration=state.iteration,
            timestamp=state.updated_at,
            event_type=event_type,
            summary=summary,
            artifact_refs=artifact_refs,
            duration_ms=duration_ms,
            usage=usage or {},
        )
        self._append_jsonl(directory / "events.jsonl", jsonable(event))
        return event

    def write_artifact(self, run_id: str, name: str, content: str | bytes) -> str:
        """Write large evidence outside events and return a run-relative reference."""

        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("artifact name must stay inside the run")
        path = self.run_dir(run_id) / "artifacts" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path.relative_to(self.run_dir(run_id)).as_posix()

    @staticmethod
    def _atomic_json(path: Path, value: Any) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    @staticmethod
    def _append_jsonl(path: Path, value: Any) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
