"""Agent 适配器；确定性的 ScriptedAgent 同时充当教学场景的行为基准。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, Sequence

from .context import AgentContext
from .types import AgentDecision, DecisionKind


class AgentAdapter(Protocol):
    """LoopEngine 依赖的最小 Agent 接口。"""

    name: str

    def next_action(self, context: AgentContext) -> AgentDecision: ...


class ScriptedAgent:
    """按顺序返回预声明动作，让教程、测试和 CI 可以稳定复现。"""

    name = "scripted"

    def __init__(self, decisions: Sequence[AgentDecision]) -> None:
        self._decisions = tuple(decisions)
        self._index = 0

    @classmethod
    def from_file(cls, path: str | Path) -> "ScriptedAgent":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("scripted agent file must contain a JSON list")
        return cls([AgentDecision.from_mapping(item) for item in payload])

    def next_action(self, context: AgentContext) -> AgentDecision:
        # 教学脚本刻意不根据上下文临场决策；真实反馈仍由 Loop 完整构造和记录。
        del context
        if self._index >= len(self._decisions):
            return AgentDecision(
                DecisionKind.BLOCKED,
                "script exhausted before verification succeeded",
                reason="no scripted actions remain",
            )
        decision = self._decisions[self._index]
        self._index += 1
        return decision

    def restore(self, completed_calls: int) -> None:
        """恢复时跳过已经持久化的调用，避免重复播放旧动作。"""

        self._index = min(max(0, completed_calls), len(self._decisions))

    @property
    def calls(self) -> int:
        return self._index
