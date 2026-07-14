"""证据驱动 Agent Loop 的唯一生命周期控制器。"""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable, Mapping

from .agent import AgentAdapter
from .context import ContextBuilder
from .policy import PolicyEngine, StopPolicy
from .storage import StateStore, jsonable
from .tools import ToolRegistry
from .types import (
    AgentDecision,
    ConfigError,
    DecisionKind,
    RunSpec,
    RunState,
    RunStatus,
    StopDecision,
    ToolResult,
    ToolStatus,
    Verdict,
)
from .verifier import PythonScriptVerifier
from .workspace import Workspace


# 状态机采用白名单：任何未显式列出的迁移都视为编程错误。
ALLOWED_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.CREATED: {
        RunStatus.VERIFYING,
        RunStatus.BUDGET_EXHAUSTED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.RUNNING: {
        RunStatus.VERIFYING,
        RunStatus.NEEDS_REVIEW,
        RunStatus.BLOCKED,
        RunStatus.BUDGET_EXHAUSTED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.VERIFYING: {
        RunStatus.RUNNING,
        RunStatus.COMPLETED,
        RunStatus.NEEDS_REVIEW,
        RunStatus.BUDGET_EXHAUSTED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.NEEDS_REVIEW: {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.FAILED},
}


class LoopEngine:
    """编排上下文、Agent、策略、工具、验证和持久化，但不替代各组件职责。"""

    def __init__(
        self,
        spec: RunSpec,
        agent: AgentAdapter,
        store: StateStore,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.spec = spec
        self.agent = agent
        self.store = store
        self.policy = PolicyEngine()
        self.stop_policy = StopPolicy()
        self.clock = clock

    def start(self, run_id: str | None = None) -> RunState:
        deadline = self.clock() + self.spec.budget.max_elapsed_seconds
        state = self.store.create(
            self.spec,
            run_id,
            {
                "agent": self.agent.name,
                "provider": getattr(self.agent, "provider_name", None),
                "model": getattr(self.agent, "model", None),
            },
        )
        workspace = Workspace.create(
            self.spec.workspace, self.store.run_dir(state.run_id) / "workspace"
        )
        return self._drive(state, workspace, deadline)

    def resume(self, run_id: str, approval: bool | None = None) -> RunState:
        state = self.store.recover(run_id)
        manifest = self.store.load_manifest(run_id)
        if manifest["scenario"]["digest"] != self.spec.digest:
            raise ConfigError("scenario changed since the run was created")
        runtime = manifest.get("runtime")
        if runtime:
            expected_provider = runtime.get("provider")
            # Provider 抽象引入前的旧 manifest 中，llm 隐含表示 OpenAI。
            if expected_provider is None and runtime.get("agent") == "llm":
                expected_provider = "openai"
            if (
                runtime.get("agent") != self.agent.name
                or expected_provider != getattr(self.agent, "provider_name", None)
                or runtime.get("model") != getattr(self.agent, "model", None)
            ):
                raise ConfigError(
                    "resume must use the Run's original Agent, provider, and model"
                )
        if state.is_terminal:
            return state

        workspace = Workspace.open(
            self.spec.workspace, self.store.run_dir(run_id) / "workspace"
        )
        approved_action: tuple[str, AgentDecision] | None = None
        if state.status is RunStatus.NEEDS_REVIEW:
            pending = state.pending_approval
            if not pending or approval is None:
                return state
            if approval is False:
                state.pending_approval = None
                self._transition(
                    state, RunStatus.CANCELLED, "approval rejected", "approval_resolved"
                )
                return state
            if pending["workspace_digest"] != workspace.digest():
                self._transition(
                    state, RunStatus.FAILED, "workspace changed after approval request"
                )
                return state
            decision = self._validate_decision(pending["decision"])
            approved_action = (str(pending["action_id"]), decision)
            state.pending_approval = None
            self._transition(
                state, RunStatus.RUNNING, "approval granted", "approval_resolved"
            )

        restore = getattr(self.agent, "restore", None)
        if callable(restore):
            restore(state.budget_usage.agent_calls)
        deadline = self.clock() + self.spec.budget.max_elapsed_seconds
        return self._drive(state, workspace, deadline, approved_action)

    def _drive(
        self,
        state: RunState,
        workspace: Workspace,
        deadline: float,
        approved_action: tuple[str, AgentDecision] | None = None,
    ) -> RunState:
        tools = ToolRegistry(workspace, self.spec.context.max_tool_output_chars)
        # 在第一次模型调用之前校验工具配置，避免先花费 Token 才发现配置错误。
        tools.definitions(self.spec.allowed_tools)
        context_builder = ContextBuilder(self.spec, tools)
        verifier = PythonScriptVerifier(self.spec, workspace, self.store)
        elapsed = lambda: self.spec.budget.max_elapsed_seconds - max(
            0.0, deadline - self.clock()
        )

        # 先验证再调用 Agent：如果初始 Workspace 已满足目标，可以零 Agent 调用完成。
        if not state.initial_verification_done and self._verify(
            state, workspace, verifier, deadline
        ):
            return state

        if approved_action and self._execute_approved_tool(
            state, workspace, tools, approved_action, deadline
        ):
            tool = tools.get(approved_action[1].tool or "")
            if tool.mutates_workspace and self._verify(state, workspace, verifier, deadline):
                return state

        # needs_review 必须把控制权交还给人；其他非终态继续进入有预算的反馈循环。
        while not state.is_terminal and state.status is not RunStatus.NEEDS_REVIEW:
            budget = self.stop_policy.before_iteration(state, self.spec, elapsed())
            self._record_stop_decision(state, budget)
            if budget.should_stop:
                self._transition(state, budget.status, budget.reason)
                break

            state.iteration += 1
            state.budget_usage.iterations += 1
            state.budget_usage.agent_calls += 1
            context = context_builder.build(state)
            self.store.checkpoint(
                state,
                "context_built",
                f"built {len(context.prompt)} character context",
                usage={"truncated": context.truncated},
            )
            try:
                decision = self._validate_decision(self.agent.next_action(context))
            except Exception as exc:
                # Provider/适配器失败也会转成可见反馈，不能绕过预算进行内部无限重试。
                self._agent_error(state, exc)
                continue

            state.last_action = decision.to_dict()
            self.store.checkpoint(
                state,
                "action_proposed",
                decision.summary,
                usage=getattr(self.agent, "last_usage", None),
            )
            time_budget = self._time_budget(deadline)
            self._record_stop_decision(state, time_budget)
            if time_budget.should_stop:
                self._transition(state, time_budget.status, time_budget.reason)
                break
            if decision.kind is DecisionKind.BLOCKED:
                self._handle_blocked(state)
                continue
            if decision.kind is DecisionKind.REQUEST_VERIFICATION:
                if self._verify(state, workspace, verifier, deadline):
                    break
                continue
            if self._execute_tool(state, workspace, tools, decision, deadline):
                tool = tools.get(decision.tool or "")
                # 只有成功修改 Workspace 的工具才自动触发验证；纯读取不浪费验证预算。
                if tool.mutates_workspace and state.last_tool_result and (
                    state.last_tool_result["status"] == ToolStatus.SUCCESS.value
                ):
                    if self._verify(state, workspace, verifier, deadline):
                        break
        return state

    def _execute_tool(
        self,
        state: RunState,
        workspace: Workspace,
        tools: ToolRegistry,
        decision: AgentDecision,
        deadline: float,
    ) -> bool:
        # action_id 让一次提议、授权、执行和结果可以在事件日志中被关联起来。
        action_id = uuid.uuid4().hex
        if decision.tool not in self.spec.allowed_tools:
            result = ToolResult(
                action_id,
                decision.tool or "",
                ToolStatus.DENIED,
                "tool is not allowed by the scenario",
                error_code="tool_not_allowed",
            )
            self._save_tool_result(state, result, "authorization_decided")
            return False

        tool = tools.get(decision.tool)
        authorization = self.policy.authorize(tool.risk, self.spec)
        self.store.checkpoint(state, "authorization_decided", authorization.reason)
        if authorization.needs_approval:
            # 审批绑定当前 Workspace 摘要；恢复时内容有变化就拒绝执行旧动作。
            state.pending_approval = {
                "action_id": action_id,
                "decision": decision.to_dict(),
                "workspace_digest": workspace.digest(),
            }
            self._transition(
                state, RunStatus.NEEDS_REVIEW, authorization.reason, "approval_requested"
            )
            return False
        if authorization.denied:
            result = ToolResult(
                action_id,
                tool.name,
                ToolStatus.DENIED,
                authorization.reason,
                error_code="policy_denied",
            )
            self._save_tool_result(state, result, "tool_completed")
            return False

        time_budget = self._time_budget(deadline)
        self._record_stop_decision(state, time_budget)
        if time_budget.should_stop:
            self._transition(state, time_budget.status, time_budget.reason)
            return False
        budget = self.stop_policy.before_tool(state, self.spec)
        self._record_stop_decision(state, budget)
        if budget.should_stop:
            self._transition(state, budget.status, budget.reason)
            return False

        state.budget_usage.tool_calls += 1
        # 工具执行前先记录 in-flight；若进程崩溃，恢复逻辑会暂停而不是盲目重放。
        state.in_flight_action = {
            "action_id": action_id,
            "decision": decision.to_dict(),
            "workspace_digest": workspace.digest(),
        }
        self.store.checkpoint(
            state, "tool_started", f"starting {tool.name} action={action_id}"
        )
        result = tools.execute(action_id, tool.name, decision.arguments)
        state.in_flight_action = None
        self._save_tool_result(state, result, "tool_completed")
        return result.status is ToolStatus.SUCCESS

    def _execute_approved_tool(
        self,
        state: RunState,
        workspace: Workspace,
        tools: ToolRegistry,
        approved_action: tuple[str, AgentDecision],
        deadline: float,
    ) -> bool:
        action_id, decision = approved_action
        if decision.tool not in self.spec.allowed_tools:
            self._transition(state, RunStatus.FAILED, "approved tool is no longer allowed")
            return False
        tool = tools.get(decision.tool)
        if tool.risk not in self.spec.policy.require_approval:
            self._transition(state, RunStatus.FAILED, "approved action has unexpected risk")
            return False
        for budget in (
            self._time_budget(deadline),
            self.stop_policy.before_tool(state, self.spec),
        ):
            self._record_stop_decision(state, budget)
            if budget.should_stop:
                self._transition(state, budget.status, budget.reason)
                return False

        state.budget_usage.tool_calls += 1
        state.in_flight_action = {
            "action_id": action_id,
            "decision": decision.to_dict(),
            "workspace_digest": workspace.digest(),
            "approved": True,
        }
        self.store.checkpoint(
            state, "tool_started", f"starting approved {tool.name} action={action_id}"
        )
        result = tools.execute(action_id, tool.name, decision.arguments)
        state.in_flight_action = None
        self._save_tool_result(state, result, "tool_completed")
        return result.status is ToolStatus.SUCCESS

    def _verify(
        self,
        state: RunState,
        workspace: Workspace,
        verifier: PythonScriptVerifier,
        deadline: float,
    ) -> bool:
        time_budget = self._time_budget(deadline)
        self._record_stop_decision(state, time_budget)
        if time_budget.should_stop:
            self._transition(state, time_budget.status, time_budget.reason)
            return True
        budget = self.stop_policy.before_verification(state, self.spec)
        self._record_stop_decision(state, budget)
        if budget.should_stop:
            self._transition(state, budget.status, budget.reason)
            return True

        state.budget_usage.verifications += 1
        self._transition(state, RunStatus.VERIFYING, "verification started", "verification_started")
        report = verifier.verify(state.run_id)
        state.initial_verification_done = True
        digest = workspace.digest()
        # 只有“失败指纹相同且 Workspace 摘要未变”才算重复失败。
        if report.verdict is Verdict.PASS:
            state.same_failure_count = 0
        elif (
            report.failure_fingerprint == state.last_failure_fingerprint
            and digest == state.last_workspace_digest
        ):
            state.same_failure_count += 1
        else:
            state.same_failure_count = 1
        state.last_failure_fingerprint = report.failure_fingerprint
        state.last_workspace_digest = digest
        state.last_verification = jsonable(report)
        self.store.checkpoint(
            state,
            "verification_completed",
            report.feedback,
            artifact_refs=report.evidence_refs,
            duration_ms=report.duration_ms,
        )

        time_budget = self._time_budget(deadline)
        self._record_stop_decision(state, time_budget)
        if time_budget.should_stop:
            self._transition(state, time_budget.status, time_budget.reason)
            return True
        decision = self.stop_policy.after_verification(
            report, state.same_failure_count, self.spec
        )
        self._record_stop_decision(state, decision)
        if decision.should_stop:
            self._transition(state, decision.status, decision.reason)
            return True
        self._transition(state, RunStatus.RUNNING, decision.reason)
        return False

    def _handle_blocked(self, state: RunState) -> None:
        result = state.last_tool_result or {}
        supporting = bool(
            result.get("iteration") == state.iteration - 1
            and result.get("tool_name") != "agent"
            and result.get("status") == ToolStatus.ERROR.value
            and result.get("error_code") in {"FileNotFoundError", "PermissionError"}
        )
        decision = self.stop_policy.blocked_proposal(supporting)
        self._record_stop_decision(state, decision)
        self._transition(state, decision.status, decision.reason)

    def _agent_error(self, state: RunState, error: Exception) -> None:
        result = ToolResult(
            uuid.uuid4().hex,
            "agent",
            ToolStatus.ERROR,
            str(error),
            error_code=type(error).__name__,
        )
        self._save_tool_result(state, result, "agent_failed")

    def _save_tool_result(self, state: RunState, result: ToolResult, event_type: str) -> None:
        state.last_tool_result = {**jsonable(result), "iteration": state.iteration}
        self.store.checkpoint(
            state, event_type, result.summary, duration_ms=result.duration_ms
        )

    def _record_stop_decision(self, state: RunState, decision) -> None:
        self.store.checkpoint(state, "stop_decided", decision.reason)

    @staticmethod
    def _validate_decision(value: Any) -> AgentDecision:
        if isinstance(value, AgentDecision):
            value = value.to_dict()
        if not isinstance(value, Mapping):
            raise ConfigError("agent adapter must return an AgentDecision or mapping")
        return AgentDecision.from_mapping(value)

    def _time_budget(self, deadline: float) -> StopDecision:
        if self.clock() >= deadline:
            return StopDecision(RunStatus.BUDGET_EXHAUSTED, "elapsed time budget exhausted")
        return StopDecision(None, "elapsed time budget available")

    def _transition(
        self,
        state: RunState,
        target: RunStatus | None,
        reason: str,
        event_type: str = "state_transitioned",
    ) -> None:
        """执行一次合法状态迁移，并立即持久化迁移原因。"""

        if target is None:
            return
        if target not in ALLOWED_TRANSITIONS.get(state.status, set()):
            raise RuntimeError(f"invalid state transition: {state.status.value} -> {target.value}")
        previous = state.status
        state.status = target
        state.stop_reason = reason if target.is_terminal or target is RunStatus.NEEDS_REVIEW else None
        self.store.checkpoint(state, event_type, f"{previous.value} -> {target.value}: {reason}")
