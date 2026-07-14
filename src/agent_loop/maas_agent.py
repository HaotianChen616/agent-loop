"""与具体 Provider 无关的 MaaS Agent 适配器。"""

from __future__ import annotations

import json

from .context import AgentContext
from .providers import MaaSProvider
from .types import AgentDecision


ADAPTER_INSTRUCTIONS = """You propose one action inside an externally controlled loop.
Use only a tool described in the input. You cannot declare success; request
verification when the evidence should be checked. Return exactly one JSON
object matching the supplied schema. The arguments field must be a JSON-encoded
object string, or "{}" when no tool is requested."""

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


class MaaSAgent:
    """把一次 Provider 响应校验并转换为 Loop 的 AgentDecision 契约。"""

    name = "llm"

    def __init__(self, provider: MaaSProvider) -> None:
        """绑定一个已配置 Provider，并暴露可冻结到 manifest 的名称与模型。"""

        self.provider = provider
        self.provider_name = provider.name
        self.model = provider.model
        self.last_usage: dict[str, int] | None = None

    def next_action(self, context: AgentContext) -> AgentDecision:
        """请求一次结构化补全，并在本地转换成 AgentDecision。

        Provider 负责厂商协议、拒绝/截断判断和 Token 字段归一化；本方法负责 JSON
        形态及 arguments 二次解析，最后仍调用 AgentDecision.from_mapping 做语义校验。
        """

        # Provider 只负责协议翻译；决策语义仍在本地统一校验，不能信任模型输出。
        response = self.provider.complete(
            instructions=ADAPTER_INSTRUCTIONS,
            prompt=context.prompt,
            schema=DECISION_SCHEMA,
        )
        self.last_usage = dict(response.usage) or None
        if not isinstance(response.output_text, str) or not response.output_text.strip():
            raise ValueError("model returned no structured decision")
        payload = json.loads(response.output_text)
        if not isinstance(payload, dict):
            raise ValueError("model decision must be a JSON object")
        # 参数在模型协议中是 JSON 字符串，解析后必须仍是对象才能交给工具层。
        encoded_arguments = payload.get("arguments")
        if not isinstance(encoded_arguments, str):
            raise ValueError("model arguments must be a JSON-encoded object string")
        arguments = json.loads(encoded_arguments)
        if not isinstance(arguments, dict):
            raise ValueError("decoded model arguments must be an object")
        payload["arguments"] = arguments
        return AgentDecision.from_mapping(payload)
