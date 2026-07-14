"""Built-in MaaS providers and their small construction factory."""

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
    """Create a configured provider without leaking protocol choices to CLI."""

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
