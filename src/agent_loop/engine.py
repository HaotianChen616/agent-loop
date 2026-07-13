"""The single lifecycle owner for an evidence-driven Agent loop."""

from __future__ import annotations

import time
import uuid

from .agent import AgentAdapter
from .context import ContextBuilder
from .policy import PolicyEngine, StopPolicy
from .storage import StateStore, jsonable
from .tools import ToolRegistry
from .types import (
    AgentDecision,
    DecisionKind,
    RunSpec,
    RunState,
    RunStatus,
    ToolResult,
    ToolStatus,
    Verdict,
)
from .verifier import PythonScriptVerifier
from .workspace import Workspace


ALLOWED_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.CREATED: {RunStatus.VERIFYING, RunStatus.FAILED, RunStatus.CANCELLED},
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
    def __init__(self, spec: RunSpec, agent: AgentAdapter, store: StateStore) -> None:
        self.spec = spec
        self.agent = agent
        self.store = store
        self.policy = PolicyEngine()
        self.stop_policy = StopPolicy()

    def start(self, run_id: str | None = None) -> RunState:
        state = self.store.create(self.spec, run_id)
        workspace = Workspace.create(
            self.spec.workspace, self.store.run_dir(state.run_id) / "workspace"
        )
        return self._drive(state, workspace)

    def _drive(self, state: RunState, workspace: Workspace) -> RunState:
        tools = ToolRegistry(workspace, self.spec.context.max_tool_output_chars)
        # Validate configured tools before the first model call.
        tools.definitions(self.spec.allowed_tools)
        context_builder = ContextBuilder(self.spec, tools)
        verifier = PythonScriptVerifier(self.spec, workspace, self.store)
        started = time.monotonic()

        if not state.initial_verification_done and self._verify(state, workspace, verifier):
            return state

        while not state.is_terminal and state.status is not RunStatus.NEEDS_REVIEW:
            budget = self.stop_policy.before_iteration(state, self.spec, time.monotonic() - started)
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
                decision = self.agent.next_action(context)
            except Exception as exc:  # Adapters convert provider failures into loop feedback.
                self._agent_error(state, exc)
                continue

            state.last_action = decision.to_dict()
            self.store.checkpoint(state, "action_proposed", decision.summary)
            if decision.kind is DecisionKind.BLOCKED:
                self._handle_blocked(state)
                continue
            if decision.kind is DecisionKind.REQUEST_VERIFICATION:
                if self._verify(state, workspace, verifier):
                    break
                continue
            if self._execute_tool(state, workspace, tools, decision):
                tool = tools.get(decision.tool or "")
                if tool.mutates_workspace and state.last_tool_result and (
                    state.last_tool_result["status"] == ToolStatus.SUCCESS.value
                ):
                    if self._verify(state, workspace, verifier):
                        break
        return state

    def _execute_tool(
        self,
        state: RunState,
        workspace: Workspace,
        tools: ToolRegistry,
        decision: AgentDecision,
    ) -> bool:
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
            state.pending_approval = {
                "action_id": action_id,
                "decision": decision.to_dict(),
                "workspace_digest": workspace.digest(),
            }
            self._transition(state, RunStatus.NEEDS_REVIEW, authorization.reason)
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

        budget = self.stop_policy.before_tool(state, self.spec)
        self._record_stop_decision(state, budget)
        if budget.should_stop:
            self._transition(state, budget.status, budget.reason)
            return False

        state.budget_usage.tool_calls += 1
        state.in_flight_action = {
            "action_id": action_id,
            "decision": decision.to_dict(),
            "workspace_digest": workspace.digest(),
        }
        self.store.checkpoint(state, "tool_started", f"starting {tool.name}")
        result = tools.execute(action_id, tool.name, decision.arguments)
        state.in_flight_action = None
        self._save_tool_result(state, result, "tool_completed")
        return result.status is ToolStatus.SUCCESS

    def _verify(
        self, state: RunState, workspace: Workspace, verifier: PythonScriptVerifier
    ) -> bool:
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
        supporting = bool(
            state.last_tool_result and state.last_tool_result.get("status") == ToolStatus.ERROR.value
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
        state.last_tool_result = jsonable(result)
        self.store.checkpoint(
            state, event_type, result.summary, duration_ms=result.duration_ms
        )

    def _record_stop_decision(self, state: RunState, decision) -> None:
        self.store.checkpoint(state, "stop_decided", decision.reason)

    def _transition(
        self,
        state: RunState,
        target: RunStatus | None,
        reason: str,
        event_type: str = "state_transitioned",
    ) -> None:
        if target is None:
            return
        if target not in ALLOWED_TRANSITIONS.get(state.status, set()):
            raise RuntimeError(f"invalid state transition: {state.status.value} -> {target.value}")
        previous = state.status
        state.status = target
        state.stop_reason = reason if target.is_terminal or target is RunStatus.NEEDS_REVIEW else None
        self.store.checkpoint(state, event_type, f"{previous.value} -> {target.value}: {reason}")
