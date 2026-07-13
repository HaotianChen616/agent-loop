"""Agent adapters.  The deterministic adapter is also the teaching oracle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, Sequence

from .context import AgentContext
from .types import AgentDecision, DecisionKind


class AgentAdapter(Protocol):
    name: str

    def next_action(self, context: AgentContext) -> AgentDecision: ...


class ScriptedAgent:
    """Return predeclared actions so tutorials and tests are reproducible."""

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
        del context  # The script is intentionally deterministic, not adaptive.
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
        """Resume after a persisted call without replaying earlier decisions."""

        self._index = min(max(0, completed_calls), len(self._decisions))

    @property
    def calls(self) -> int:
        return self._index
