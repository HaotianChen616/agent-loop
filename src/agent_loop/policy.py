"""动作授权、预算检查，以及基于验证证据的停止决策。"""

from __future__ import annotations

from dataclasses import dataclass

from .types import RiskLevel, RunSpec, RunState, RunStatus, StopDecision, Verdict, VerificationReport


@dataclass(frozen=True)
class Authorization:
    """一次风险授权结论；allowed 与 needs_approval 不会同时为真。"""

    allowed: bool
    needs_approval: bool
    reason: str

    @property
    def denied(self) -> bool:
        """既未允许、也不等待审批时，动作即被策略明确拒绝。"""

        return not self.allowed and not self.needs_approval


class PolicyEngine:
    """把工具风险映射为自动允许、人工审批或拒绝。"""

    def authorize(self, risk: RiskLevel, spec: RunSpec) -> Authorization:
        """按 deny、require_approval、auto_allow 的优先级判断工具风险。"""

        if risk in spec.policy.deny:
            return Authorization(False, False, f"risk {risk.value} is denied")
        if risk in spec.policy.require_approval:
            return Authorization(False, True, f"risk {risk.value} requires approval")
        if risk in spec.policy.auto_allow:
            return Authorization(True, False, f"risk {risk.value} is auto-allowed")
        return Authorization(False, False, f"risk {risk.value} is not configured")


class StopPolicy:
    """只返回决策而不修改状态，确保状态迁移始终由 LoopEngine 统一负责。"""

    def before_iteration(self, state: RunState, spec: RunSpec, elapsed_seconds: float) -> StopDecision:
        """在调用 Agent 前检查总耗时、迭代数和 Agent 调用数。"""

        usage, limits = state.budget_usage, spec.budget
        if elapsed_seconds >= limits.max_elapsed_seconds:
            return StopDecision(RunStatus.BUDGET_EXHAUSTED, "elapsed time budget exhausted")
        if usage.iterations >= limits.max_iterations or usage.agent_calls >= limits.max_agent_calls:
            return StopDecision(RunStatus.BUDGET_EXHAUSTED, "agent iteration budget exhausted")
        return StopDecision(None, "iteration budget available")

    def before_tool(self, state: RunState, spec: RunSpec) -> StopDecision:
        """在工具副作用前检查工具调用预算。"""

        if state.budget_usage.tool_calls >= spec.budget.max_tool_calls:
            return StopDecision(RunStatus.BUDGET_EXHAUSTED, "tool call budget exhausted")
        return StopDecision(None, "tool budget available")

    def before_verification(self, state: RunState, spec: RunSpec) -> StopDecision:
        """在创建快照和子进程前检查验证次数预算。"""

        if state.budget_usage.verifications >= spec.budget.max_verifications:
            return StopDecision(RunStatus.BUDGET_EXHAUSTED, "verification budget exhausted")
        return StopDecision(None, "verification budget available")

    def after_verification(
        self, report: VerificationReport, same_failure_count: int, spec: RunSpec
    ) -> StopDecision:
        """根据验证结论、可重试性和重复失败次数决定完成、复核或继续。"""

        if report.verdict is Verdict.PASS:
            return StopDecision(RunStatus.COMPLETED, "all acceptance criteria passed")
        if report.verdict is Verdict.INCONCLUSIVE:
            return StopDecision(RunStatus.NEEDS_REVIEW, "verification was inconclusive")
        if not report.retryable:
            return StopDecision(RunStatus.FAILED, "verification failure is not retryable")
        if same_failure_count >= spec.budget.max_same_failure:
            return StopDecision(RunStatus.FAILED, "same verification failure repeated")
        return StopDecision(None, "verification failed with retry budget remaining")

    def blocked_proposal(self, has_supporting_evidence: bool) -> StopDecision:
        """只有新近工具错误能够支持自动进入 blocked，否则交给人工复核。"""

        if has_supporting_evidence:
            return StopDecision(RunStatus.BLOCKED, "blocked proposal supported by tool evidence")
        return StopDecision(RunStatus.NEEDS_REVIEW, "blocked proposal lacks external evidence")
