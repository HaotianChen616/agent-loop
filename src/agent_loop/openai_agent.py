"""Optional OpenAI Responses adapter for non-deterministic experiments."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .context import AgentContext
from .types import AgentDecision


ADAPTER_INSTRUCTIONS = """You propose one action inside an externally controlled loop.
Use only a tool described in the input. You cannot declare success; request
verification when the evidence should be checked. The arguments field must be
a JSON-encoded object string, or "{}" when no tool is requested."""

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["tool_call", "request_verification", "blocked"],
        },
        "summary": {"type": "string"},
        "tool": {"type": ["string", "null"]},
        "arguments": {
            "type": "string",
            "description": "A JSON-encoded object; use {} when no tool is requested.",
        },
        "reason": {"type": ["string", "null"]},
    },
    "required": ["kind", "summary", "tool", "arguments", "reason"],
    "additionalProperties": False,
}


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _find_refusal(response: Any) -> str | None:
    for item in _field(response, "output", ()) or ():
        for part in _field(item, "content", ()) or ():
            if _field(part, "type") == "refusal":
                return str(_field(part, "refusal", "model refused the request"))
    return None


class OpenAIResponsesAgent:
    """Ask Responses API for a bounded structured AgentDecision."""

    name = "llm"

    def __init__(
        self,
        model: str,
        request_timeout_seconds: int,
        max_output_tokens: int,
        *,
        client: Any | None = None,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("an explicit OpenAI model is required")
        if request_timeout_seconds <= 0 or max_output_tokens <= 0:
            raise ValueError("request timeout and output limit must be positive")
        self.model = model.strip()
        self.request_timeout_seconds = request_timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.last_usage: dict[str, int] | None = None
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("install agent-loop[openai] to use --agent llm") from exc
            # Retries belong to the outer loop, where they are budgeted and audited.
            client = OpenAI(max_retries=0, timeout=request_timeout_seconds)
        self.client = client

    def next_action(self, context: AgentContext) -> AgentDecision:
        response = self.client.responses.create(
            model=self.model,
            instructions=ADAPTER_INSTRUCTIONS,
            input=context.prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "agent_decision",
                    "strict": True,
                    "schema": DECISION_SCHEMA,
                }
            },
            max_output_tokens=self.max_output_tokens,
            store=False,
            timeout=self.request_timeout_seconds,
        )
        refusal = _find_refusal(response)
        if refusal:
            raise ValueError(f"model refusal: {refusal[:200]}")
        status = _field(response, "status")
        if status != "completed":
            details = _field(response, "incomplete_details")
            reason = _field(details, "reason", "unknown")
            raise ValueError(f"model response was {status or 'unknown'}: {reason}")

        usage = _field(response, "usage")
        if usage is not None:
            values = {
                name: _field(usage, name)
                for name in ("input_tokens", "output_tokens", "total_tokens")
            }
            self.last_usage = {
                name: value for name, value in values.items() if isinstance(value, int)
            } or None

        output = _field(response, "output_text")
        if not isinstance(output, str) or not output.strip():
            raise ValueError("model returned no structured decision")
        payload = json.loads(output)
        if not isinstance(payload, dict):
            raise ValueError("model decision must be a JSON object")
        encoded_arguments = payload.get("arguments")
        if not isinstance(encoded_arguments, str):
            raise ValueError("model arguments must be a JSON-encoded object string")
        arguments = json.loads(encoded_arguments)
        if not isinstance(arguments, dict):
            raise ValueError("decoded model arguments must be an object")
        payload["arguments"] = arguments
        return AgentDecision.from_mapping(payload)
