"""Stable data contracts shared by the loop components.

The control loop intentionally exchanges plain, serializable values.  This
keeps runs inspectable and lets experiments replace one component at a time.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping


class AgentLoopError(Exception):
    """Base class for expected, user-facing loop errors."""


class ConfigError(AgentLoopError):
    """Raised when a scenario is incomplete or internally inconsistent."""


class PathViolation(AgentLoopError):
    """Raised when a tool attempts to leave or mutate a protected workspace."""


class RunStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"
    BUDGET_EXHAUSTED = "budget_exhausted"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            self.COMPLETED,
            self.BLOCKED,
            self.BUDGET_EXHAUSTED,
            self.FAILED,
            self.CANCELLED,
        }


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class DecisionKind(str, Enum):
    TOOL_CALL = "tool_call"
    REQUEST_VERIFICATION = "request_verification"
    BLOCKED = "blocked"


class RiskLevel(str, Enum):
    READ = "read"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"
    IRREVERSIBLE = "irreversible"


class ToolStatus(str, Enum):
    SUCCESS = "success"
    ERROR = "error"
    DENIED = "denied"
    NEEDS_APPROVAL = "needs_approval"


@dataclass(frozen=True)
class ContextSpec:
    max_input_chars: int = 30_000
    max_history_items: int = 8
    max_tool_output_chars: int = 8_000


@dataclass(frozen=True)
class WorkspaceSpec:
    seed: str
    mode: str = "copy"
    read_only: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentSpec:
    kind: str = "scripted"
    request_timeout_seconds: int = 30
    max_output_tokens: int = 1_000
    model: str | None = None


@dataclass(frozen=True)
class VerificationSpec:
    script: str
    kind: str = "python_script"
    timeout_seconds: int = 10


@dataclass(frozen=True)
class BudgetLimits:
    max_iterations: int = 6
    max_agent_calls: int = 6
    max_tool_calls: int = 10
    max_verifications: int = 7
    max_elapsed_seconds: int = 120
    max_same_failure: int = 2


@dataclass
class BudgetUsage:
    iterations: int = 0
    agent_calls: int = 0
    tool_calls: int = 0
    verifications: int = 0


@dataclass(frozen=True)
class PolicySpec:
    auto_allow: tuple[RiskLevel, ...] = (
        RiskLevel.READ,
        RiskLevel.LOCAL_WRITE,
    )
    require_approval: tuple[RiskLevel, ...] = (RiskLevel.EXTERNAL_WRITE,)
    deny: tuple[RiskLevel, ...] = (RiskLevel.IRREVERSIBLE,)


@dataclass(frozen=True)
class RunSpec:
    schema_version: int
    scenario_id: str
    title: str
    learning_objective: str
    goal: str
    acceptance_criteria: tuple[str, ...]
    instructions: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    context: ContextSpec
    workspace: WorkspaceSpec
    agent: AgentSpec
    verification: VerificationSpec
    budget: BudgetLimits
    policy: PolicySpec
    scenario_root: str
    digest: str


@dataclass(frozen=True)
class AgentDecision:
    kind: DecisionKind
    summary: str
    tool: str | None = None
    arguments: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AgentDecision":
        """Validate untrusted agent output before it reaches policy or tools."""

        try:
            kind = DecisionKind(value["kind"])
            summary = str(value["summary"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError("agent decision requires valid kind and summary") from exc
        if not summary:
            raise ConfigError("agent decision summary cannot be empty")

        allowed = {"kind", "summary", "tool", "arguments", "reason"}
        unknown = set(value) - allowed
        if unknown:
            raise ConfigError(f"unknown agent decision fields: {sorted(unknown)}")

        tool = value.get("tool")
        arguments = value.get("arguments", {})
        if kind is DecisionKind.TOOL_CALL:
            if not isinstance(tool, str) or not tool:
                raise ConfigError("tool_call requires a tool name")
            if not isinstance(arguments, Mapping):
                raise ConfigError("tool_call arguments must be an object")
        elif tool is not None or arguments:
            raise ConfigError(f"{kind.value} cannot include a tool call")

        return cls(kind, summary, tool, dict(arguments), value.get("reason"))

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "kind": self.kind.value}


@dataclass(frozen=True)
class ToolResult:
    action_id: str
    tool_name: str
    status: ToolStatus
    summary: str
    artifact_refs: tuple[str, ...] = ()
    error_code: str | None = None
    duration_ms: int = 0
    output_truncated: bool = False


@dataclass(frozen=True)
class CriterionResult:
    criterion_id: str
    verdict: Verdict
    message: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationReport:
    verdict: Verdict
    criteria_results: tuple[CriterionResult, ...]
    feedback: str
    evidence_refs: tuple[str, ...] = ()
    failure_fingerprint: str | None = None
    retryable: bool = True
    duration_ms: int = 0


@dataclass(frozen=True)
class StopDecision:
    status: RunStatus | None
    reason: str

    @property
    def should_stop(self) -> bool:
        return self.status is not None


@dataclass
class RunState:
    schema_version: int
    run_id: str
    scenario_id: str
    scenario_digest: str
    status: RunStatus = RunStatus.CREATED
    iteration: int = 0
    initial_verification_done: bool = False
    started_at: str = ""
    updated_at: str = ""
    budget_usage: BudgetUsage = field(default_factory=BudgetUsage)
    last_action: dict[str, Any] | None = None
    last_tool_result: dict[str, Any] | None = None
    last_verification: dict[str, Any] | None = None
    last_failure_fingerprint: str | None = None
    last_workspace_digest: str | None = None
    same_failure_count: int = 0
    pending_approval: dict[str, Any] | None = None
    in_flight_action: dict[str, Any] | None = None
    stop_reason: str | None = None
    revision: int = 0
    event_sequence: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal


@dataclass(frozen=True)
class LoopEvent:
    event_id: str
    sequence: int
    state_revision: int
    run_id: str
    iteration: int
    timestamp: str
    event_type: str
    summary: str
    artifact_refs: tuple[str, ...] = ()
    duration_ms: int = 0
    usage: Mapping[str, Any] = field(default_factory=dict)
