"""Loop 各组件共享的稳定数据契约。

控制循环刻意只交换可序列化的普通数据，使每次运行都能被检查、持久化，
也便于在方案实验中一次替换一个组件，而不牵动其他部分。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping


class AgentLoopError(Exception):
    """可预期且可以直接展示给用户的 Loop 错误基类。"""


class ConfigError(AgentLoopError):
    """Scenario 不完整或内部配置互相冲突。"""


class PathViolation(AgentLoopError):
    """工具试图逃出 Workspace 或修改受保护路径。"""


class ApplyError(AgentLoopError):
    """已完成的 Run 无法安全发布到目标目录。"""


class RunStatus(str, Enum):
    """Run 的生命周期状态。

    `created/running/verifying/needs_review` 表示仍可继续推进；其余状态为终态。
    状态之间不能任意跳转，合法迁移由 `engine.ALLOWED_TRANSITIONS` 集中定义。
    """

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
        """返回当前状态是否已经结束，不再允许 Loop 自动推进。"""

        return self in {
            self.COMPLETED,
            self.BLOCKED,
            self.BUDGET_EXHAUSTED,
            self.FAILED,
            self.CANCELLED,
        }


class Verdict(str, Enum):
    """Verifier 对单项标准或整体验证给出的三态结论。"""

    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class DecisionKind(str, Enum):
    """Agent 每轮唯一允许返回的决策类型。"""

    TOOL_CALL = "tool_call"
    REQUEST_VERIFICATION = "request_verification"
    BLOCKED = "blocked"


class RiskLevel(str, Enum):
    """工具副作用风险，PolicyEngine 根据它决定授权方式。"""

    READ = "read"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"
    IRREVERSIBLE = "irreversible"


class ToolStatus(str, Enum):
    """一次工具尝试的结果；它描述执行事实，不代表 Run 已完成。"""

    SUCCESS = "success"
    ERROR = "error"
    DENIED = "denied"
    NEEDS_APPROVAL = "needs_approval"


@dataclass(frozen=True)
class ContextSpec:
    """上下文与工具输出的大小上限，防止历史和观察结果无限膨胀。"""

    max_input_chars: int = 30_000
    max_history_items: int = 8
    max_tool_output_chars: int = 8_000


@dataclass(frozen=True)
class WorkspaceSpec:
    """Workspace 种子目录、复制模式和不可写的相对路径。"""

    seed: str
    mode: str = "copy"
    read_only: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentSpec:
    """Agent 类型及真实模型调用所需的 Provider、模型和请求边界。"""

    kind: str = "scripted"
    request_timeout_seconds: int = 30
    max_output_tokens: int = 1_000
    model: str | None = None
    script: str | None = None
    provider: str | None = None


@dataclass(frozen=True)
class VerificationSpec:
    """可信验证脚本及其独立进程超时时间。"""

    script: str
    kind: str = "python_script"
    timeout_seconds: int = 10


@dataclass(frozen=True)
class BudgetLimits:
    """单个 Run 的硬预算；每个维度都由 LoopEngine 在副作用前检查。"""

    max_iterations: int = 6
    max_agent_calls: int = 6
    max_tool_calls: int = 10
    max_verifications: int = 7
    max_elapsed_seconds: int = 120
    max_same_failure: int = 2


@dataclass
class BudgetUsage:
    """当前 Run 已消费的预算计数，随 state.json 一起持久化。"""

    iterations: int = 0
    agent_calls: int = 0
    tool_calls: int = 0
    verifications: int = 0


@dataclass(frozen=True)
class PolicySpec:
    """三组互斥的风险策略：自动允许、需要审批和始终拒绝。"""

    auto_allow: tuple[RiskLevel, ...] = (
        RiskLevel.READ,
        RiskLevel.LOCAL_WRITE,
    )
    require_approval: tuple[RiskLevel, ...] = (RiskLevel.EXTERNAL_WRITE,)
    deny: tuple[RiskLevel, ...] = (RiskLevel.IRREVERSIBLE,)


@dataclass(frozen=True)
class RunSpec:
    """从 Scenario 校验后冻结的完整运行定义。

    路径都已解析为绝对路径，`digest` 绑定 Scenario 控制目录内容。运行期间只依赖
    该对象，不再信任原始 TOML 中未校验的值。
    """

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
    scenario_file: str
    digest: str


@dataclass(frozen=True)
class AgentDecision:
    """Agent 的单轮提议。

    tool_call 携带工具与参数；request_verification 和 blocked 不得夹带工具调用。
    该对象只表达意图，是否授权和执行仍由 LoopEngine 决定。
    """

    kind: DecisionKind
    summary: str
    tool: str | None = None
    arguments: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AgentDecision":
        """在 Agent 输出进入策略层或工具层之前，将其当作不可信数据校验。"""

        try:
            kind = DecisionKind(value["kind"])
            summary = str(value["summary"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError("agent decision requires valid kind and summary") from exc
        if not summary:
            raise ConfigError("agent decision summary cannot be empty")

        # 拒绝未知字段，避免模型借由扩展字段向下游偷偷传递未定义语义。
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

        reason = value.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ConfigError("agent decision reason must be a string")
        try:
            json.dumps(dict(arguments))
        except (TypeError, ValueError) as exc:
            raise ConfigError("tool arguments must be JSON serializable") from exc

        return cls(kind, summary, tool, dict(arguments), reason)

    def to_dict(self) -> dict[str, Any]:
        """转换为稳定的 JSON 形态，供状态、事件和审批记录持久化。"""

        return {**asdict(self), "kind": self.kind.value}


@dataclass(frozen=True)
class ToolResult:
    """工具执行的结构化事实，包含动作标识、输出、错误和截断信息。"""

    action_id: str
    tool_name: str
    status: ToolStatus
    summary: str
    output: Any = None
    artifact_refs: tuple[str, ...] = ()
    error_code: str | None = None
    duration_ms: int = 0
    output_truncated: bool = False


@dataclass(frozen=True)
class CriterionResult:
    """Verifier 对一个 acceptance criterion 的独立判断。"""

    criterion_id: str
    verdict: Verdict
    message: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationReport:
    """一次完整验证的结果、反馈、证据引用和重复失败指纹。"""

    verdict: Verdict
    criteria_results: tuple[CriterionResult, ...]
    feedback: str
    evidence_refs: tuple[str, ...] = ()
    failure_fingerprint: str | None = None
    retryable: bool = True
    duration_ms: int = 0


@dataclass(frozen=True)
class StopDecision:
    """StopPolicy 的纯函数结果；`status=None` 表示允许继续。"""

    status: RunStatus | None
    reason: str

    @property
    def should_stop(self) -> bool:
        """是否要求 LoopEngine 迁移到一个停止或人工复核状态。"""

        return self.status is not None


@dataclass
class RunState:
    """可恢复的 Run 当前事实快照。

    `last_*` 保存最近证据，`pending_approval/in_flight_action` 防止恢复时重复副作用，
    `revision/event_sequence` 用于协调 state.json 与 events.jsonl。
    """

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
        """代理给 RunStatus，便于主循环直接判断是否退出。"""

        return self.status.is_terminal


@dataclass(frozen=True)
class LoopEvent:
    """一条追加式审计事件，只保存摘要和证据引用，不复制完整状态。"""

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
