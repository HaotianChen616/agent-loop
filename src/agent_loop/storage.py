"""持久化 Run 状态快照、追加式事件日志和大体积证据。"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, fields
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .types import BudgetUsage, LoopEvent, RunSpec, RunState, RunStatus


def utc_now() -> str:
    """返回带 UTC 时区的 ISO 8601 时间，供状态和事件统一使用。"""

    return datetime.now(UTC).isoformat()


def jsonable(value: Any) -> Any:
    """把 dataclass 和 Enum 转换成稳定、可写入 JSON 的普通值。"""

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
    """以 state.json 作为恢复事实，以 events.jsonl 作为追加式审计轨迹。"""

    def __init__(
        self,
        root: str | Path = ".agent-loop/runs",
        event_listener: Callable[[LoopEvent], None] | None = None,
    ) -> None:
        """绑定 Runs 根目录，并可选注册实时事件监听器。"""

        self.root = Path(root).resolve()
        self.event_listener = event_listener

    def run_dir(self, run_id: str) -> Path:
        """验证稳定 run_id 格式并返回对应目录，防止借 ID 进行路径穿越。"""

        if not run_id or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in run_id):
            raise ValueError("run_id must contain lowercase letters, digits, and hyphens")
        return self.root / run_id

    def create(
        self,
        spec: RunSpec,
        run_id: str | None = None,
        runtime: dict[str, Any] | None = None,
    ) -> RunState:
        """初始化新 Run 的 manifest、state、events 和 artifacts 目录。

        `runtime` 记录实际使用的 Agent/Provider/model，而不是只记录 Scenario 默认值；
        这使命令行覆盖项也能在 resume 时被严格复核。
        """

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
            "runtime": runtime
            or {
                "agent": spec.agent.kind,
                "provider": spec.agent.provider,
                "model": spec.agent.model,
            },
        }
        self._atomic_json(directory / "manifest.json", manifest)
        self.checkpoint(state, "run_started", f"started scenario {spec.scenario_id}")
        return state

    def load(self, run_id: str) -> RunState:
        """从 state.json 恢复强类型 RunState，并拒绝未知字段。"""

        data = json.loads((self.run_dir(run_id) / "state.json").read_text(encoding="utf-8"))
        allowed = {item.name for item in fields(RunState)}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"state contains unknown fields: {sorted(unknown)}")
        data["status"] = RunStatus(data["status"])
        data["budget_usage"] = BudgetUsage(**data.get("budget_usage", {}))
        return RunState(**data)

    def load_manifest(self, run_id: str) -> dict[str, Any]:
        """读取创建时冻结的 Scenario 与运行身份。"""

        return json.loads(
            (self.run_dir(run_id) / "manifest.json").read_text(encoding="utf-8")
        )

    def read_events(self, run_id: str) -> tuple[dict[str, Any], ...]:
        """按追加顺序读取完整事件日志，主要供 inspect 和恢复检查使用。"""

        path = self.run_dir(run_id) / "events.jsonl"
        return tuple(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())

    def recover(self, run_id: str) -> RunState:
        """协调事件尾部与状态快照，但绝不通过重放事件来重复副作用。

        state 比事件领先时补记恢复事件；末尾半条 JSON 可安全截断；事件领先、
        中部损坏或存在 in-flight 动作时进入 needs_review，把不确定性交给人处理。
        """

        state = self.load(run_id)
        path = self.run_dir(run_id) / "events.jsonl"
        raw_lines = path.read_text(encoding="utf-8").splitlines()
        valid: list[dict[str, Any]] = []
        truncated_tail = False
        corrupt_middle = False
        for index, line in enumerate(raw_lines):
            try:
                valid.append(json.loads(line))
            except json.JSONDecodeError:
                if index == len(raw_lines) - 1:
                    truncated_tail = True
                else:
                    corrupt_middle = True
                break

        if truncated_tail:
            # checkpoint 总是先提交 state，因此不完整的最后一条事件可以安全丢弃。
            path.write_text(
                "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in valid),
                encoding="utf-8",
            )

        last_revision = max((int(item.get("state_revision", 0)) for item in valid), default=0)
        last_sequence = max((int(item.get("sequence", 0)) for item in valid), default=0)
        reasons: list[str] = []
        if state.revision > last_revision:
            reasons.append("state snapshot was ahead of the event log")
        if truncated_tail:
            reasons.append("truncated an incomplete event tail")
        if corrupt_middle or last_revision > state.revision:
            state.revision = max(state.revision, last_revision)
            state.event_sequence = max(state.event_sequence, last_sequence)
            state.status = RunStatus.NEEDS_REVIEW
            state.stop_reason = "event log is ahead of or corrupt relative to state"
            reasons.append(state.stop_reason)
        if state.in_flight_action:
            # 工具可能已经产生副作用但尚未来得及记录结果，自动重试会造成重复执行。
            state.status = RunStatus.NEEDS_REVIEW
            state.stop_reason = "an in-flight action has an unknown result"
            reasons.append(state.stop_reason)

        if reasons:
            self.checkpoint(state, "recovery_performed", "; ".join(reasons))
        return state

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
        """先原子提交状态，再追加一条指向该 revision 的事件。

        这种顺序保证崩溃时最多出现“state 领先一条事件”，不会出现事件宣称了尚未
        持久化的状态。文件写入均 flush + fsync，随后才通知实时监听器。
        """

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
        if self.event_listener:
            self.event_listener(event)
        return event

    def write_artifact(self, run_id: str, name: str, content: str | bytes) -> str:
        """把大体积证据写在事件之外，并返回 Run 内的相对引用。"""

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

    def write_application(
        self, run_id: str, application_id: str, record: dict[str, Any]
    ) -> str:
        """原子持久化 Apply 审计记录，但不改变已经终止的 Run 状态。"""

        if not application_id or not application_id.isalnum():
            raise ValueError("application_id must be alphanumeric")
        relative = Path("applications") / f"{application_id}.json"
        path = self.run_dir(run_id) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_json(path, record)
        return relative.as_posix()

    @staticmethod
    def _atomic_json(path: Path, value: Any) -> None:
        """写临时文件、fsync 后使用 os.replace 原子替换正式 JSON。"""

        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    @staticmethod
    def _append_jsonl(path: Path, value: Any) -> None:
        """追加一行完整 JSON 并 fsync；恢复逻辑可识别末尾半写记录。"""

        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
