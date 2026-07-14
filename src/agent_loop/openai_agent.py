"""基于 Provider 的 OpenAI Agent 兼容层。"""

from __future__ import annotations

from typing import Any

from .maas_agent import MaaSAgent
from .providers.openai import OpenAIResponsesProvider


class OpenAIResponsesAgent(MaaSAgent):
    """保留 v0 的公开构造方式，内部实现委托给 OpenAI Provider。"""

    def __init__(
        self,
        model: str,
        request_timeout_seconds: int,
        max_output_tokens: int,
        *,
        client: Any | None = None,
    ) -> None:
        """按旧签名构造 OpenAIResponsesProvider，便于已有调用方平滑迁移。"""

        super().__init__(
            OpenAIResponsesProvider(
                model,
                request_timeout_seconds,
                max_output_tokens,
                client=client,
            )
        )
