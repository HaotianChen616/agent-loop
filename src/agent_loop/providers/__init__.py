"""内置 MaaS Provider 及其最小构造工厂。"""

from __future__ import annotations

from typing import Any

from .base import MaaSProvider, MaaSResponse
from .openai import OpenAIResponsesProvider
from .zhipu import ZhipuCodingPlanProvider


PROVIDER_NAMES = ("openai", "zhipu-coding-plan")


def create_provider(
    name: str,
    model: str,
    request_timeout_seconds: int,
    max_output_tokens: int,
    **kwargs: Any,
) -> MaaSProvider:
    """创建配置好的 Provider，不把厂商协议差异泄漏到 CLI。

    名称集合保持封闭，使 TOML、CLI choices、manifest 和恢复检查共享同一稳定标识。
    `kwargs` 主要用于测试注入 fake client，不经过 Scenario 配置透传。
    """

    if name == "openai":
        return OpenAIResponsesProvider(
            model, request_timeout_seconds, max_output_tokens, **kwargs
        )
    if name == "zhipu-coding-plan":
        return ZhipuCodingPlanProvider(
            model, request_timeout_seconds, max_output_tokens, **kwargs
        )
    raise ValueError(f"unknown MaaS provider: {name}")


__all__ = [
    "MaaSProvider",
    "MaaSResponse",
    "OpenAIResponsesProvider",
    "PROVIDER_NAMES",
    "ZhipuCodingPlanProvider",
    "create_provider",
]
