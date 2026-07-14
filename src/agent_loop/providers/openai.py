"""MaaS 边界的 OpenAI Responses API 实现。"""

from __future__ import annotations

from typing import Any, Mapping

from .base import MaaSResponse, field_value, normalized_usage


def _find_refusal(response: Any) -> str | None:
    """遍历 Responses 内容块，提取第一条模型拒绝信息。"""

    for item in field_value(response, "output", ()) or ():
        for part in field_value(item, "content", ()) or ():
            if field_value(part, "type") == "refusal":
                return str(field_value(part, "refusal", "model refused the request"))
    return None


class OpenAIResponsesProvider:
    """通过 OpenAI Responses API 请求严格结构化输出。"""

    name = "openai"

    def __init__(
        self,
        model: str,
        request_timeout_seconds: int,
        max_output_tokens: int,
        *,
        client: Any | None = None,
    ) -> None:
        """校验请求边界，并按需创建关闭 SDK 重试的 OpenAI 客户端。"""

        if not isinstance(model, str) or not model.strip():
            raise ValueError("an explicit OpenAI model is required")
        if request_timeout_seconds <= 0 or max_output_tokens <= 0:
            raise ValueError("request timeout and output limit must be positive")
        self.model = model.strip()
        self.request_timeout_seconds = request_timeout_seconds
        self.max_output_tokens = max_output_tokens
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("install agent-loop[openai] to use the OpenAI provider") from exc
            # 重试属于 LoopEngine，必须受到预算约束并出现在审计事件中。
            client = OpenAI(max_retries=0, timeout=request_timeout_seconds)
        self.client = client

    def complete(
        self,
        *,
        instructions: str,
        prompt: str,
        schema: Mapping[str, Any],
    ) -> MaaSResponse:
        """调用 Responses API，并只接受 completed 的严格 JSON Schema 输出。

        请求禁用服务端存储，显式设置超时和输出 Token 上限；拒绝、不完整状态和空
        文本都转换为异常，由外层 Loop 记录并受重试预算约束。
        """

        response = self.client.responses.create(
            model=self.model,
            instructions=instructions,
            input=prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "agent_decision",
                    "strict": True,
                    "schema": dict(schema),
                }
            },
            max_output_tokens=self.max_output_tokens,
            store=False,
            timeout=self.request_timeout_seconds,
        )
        # 拒绝和不完整输出都不能被当作合法动作送入外层 Loop。
        refusal = _find_refusal(response)
        if refusal:
            raise ValueError(f"model refusal: {refusal[:200]}")
        status = field_value(response, "status")
        if status != "completed":
            details = field_value(response, "incomplete_details")
            reason = field_value(details, "reason", "unknown")
            raise ValueError(f"model response was {status or 'unknown'}: {reason}")

        output = field_value(response, "output_text")
        if not isinstance(output, str) or not output.strip():
            raise ValueError("model returned no structured decision")
        usage = normalized_usage(
            field_value(response, "usage"),
            input_name="input_tokens",
            output_name="output_tokens",
        )
        return MaaSResponse(output, usage)
