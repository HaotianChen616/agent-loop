"""Compatibility wrapper for the provider-based OpenAI Agent."""

from __future__ import annotations

from typing import Any

from .maas_agent import MaaSAgent
from .providers.openai import OpenAIResponsesProvider


class OpenAIResponsesAgent(MaaSAgent):
    """Preserve the v0 public constructor while delegating to a Provider."""

    def __init__(
        self,
        model: str,
        request_timeout_seconds: int,
        max_output_tokens: int,
        *,
        client: Any | None = None,
    ) -> None:
        super().__init__(
            OpenAIResponsesProvider(
                model,
                request_timeout_seconds,
                max_output_tokens,
                client=client,
            )
        )
