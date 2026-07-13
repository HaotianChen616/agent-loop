"""Authorization, budgets, and evidence-based stop decisions."""

from __future__ import annotations

from dataclasses import dataclass

from .types import RiskLevel, RunSpec, RunState, RunStatus, StopDecision, Verdict, VerificationReport


@dataclass(frozen=True)
class Authorization:
    allowed: bool
    needs_approval: bool
    reason: str

    @property
    def denied(self) -> bool:
        return not self.allowed and not self.needs_approval


class PolicyEngine:
    def authorize(self, risk: RiskLevel, spec: RunSpec) -> Authorization:
        if risk in spec.policy.deny:
            return Authorization(False, False, f"risk {risk.value} is denied")
        if risk in spec.policy.require_approval:
            return Authorization(False, True, f"risk {risk.value} requires approval")
        if risk in spec.policy.auto_allow:
            return Authorization(True, False, f"risk {risk.value} is auto-allowed")
        return Authorization(False, False, f"risk {risk.value} is not configured")


class StopPolicy:
    """Return decisions without mutating state so transitions stay in LoopEngine."""

    def before_iteration(self, state: RunState, spec: RunSpec, elapsed_seconds: float) -> StopDecision:
        usage, limits = state.budget_usage, spec.budget
        if elapsed_seconds >= limits.max_elapsed_seconds:
            return StopDecision(RunStatus.BUDGET_EXHAUSTED, "elapsed time budget exhausted")
        if usage.iterations >= limits.max_iterations or usage.agent_calls >= limits.max_agent_calls:
            return StopDecision(RunStatus.BUDGET_EXHAUSTED, "agent iteration budget exhausted")
        return StopDecision(None, "iteration budget available")

    def before_tool(self, state: RunState, spec: RunSpec) -> StopDecision:
        if state.budget_usage.tool_calls >= spec.budget.max_tool_calls:
            return StopDecision(RunStatus.BUDGET_EXHAUSTED, "tool call budget exhausted")
        return StopDecision(None, "tool budget available")

    def before_verification(self, state: RunState, spec: RunSpec) -> StopDecision:
        if state.budget_usage.verifications >= spec.budget.max_verifications:
            return StopDecision(RunStatus.BUDGET_EXHAUSTED, "verification budget exhausted")
        return StopDecision(None, "verification budget available")

    def after_verification(
        self, report: VerificationReport, same_failure_count: int, spec: RunSpec
    ) -> StopDecision:
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
        if has_supporting_evidence:
            return StopDecision(RunStatus.BLOCKED, "blocked proposal supported by tool evidence")
        return StopDecision(RunStatus.NEEDS_REVIEW, "blocked proposal lacks external evidence")
