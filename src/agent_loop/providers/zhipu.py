"""Zhipu GLM Coding Plan implementation of the MaaS boundary."""

from __future__ import annotations

import json
import os
from typing import Any, Mapping

from .base import MaaSResponse, field_value, normalized_usage


ZHIPU_CODING_BASE_URL = "https://open.bigmodel.cn/api/coding/paas/v4"


class ZhipuCodingPlanProvider:
    """Call GLM Coding Plan through its OpenAI Chat Completions endpoint."""

    name = "zhipu-coding-plan"

    def __init__(
        self,
        model: str,
        request_timeout_seconds: int,
        max_output_tokens: int,
        *,
        api_key: str | None = None,
        base_url: str = ZHIPU_CODING_BASE_URL,
        client: Any | None = None,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("an explicit Zhipu model is required")
        if request_timeout_seconds <= 0 or max_output_tokens <= 0:
            raise ValueError("request timeout and output limit must be positive")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("Zhipu base URL cannot be empty")
        self.model = model.strip()
        self.request_timeout_seconds = request_timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.base_url = base_url.rstrip("/")
        if client is None:
            key = api_key or os.getenv("ZAI_API_KEY")
            if not key:
                raise RuntimeError("set ZAI_API_KEY to use the Zhipu Coding Plan provider")
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("install agent-loop[zhipu] to use the Zhipu provider") from exc
            # The Coding endpoint implements OpenAI Chat Completions. Keeping
            # SDK retries off makes failures visible to the bounded outer loop.
            client = OpenAI(
                api_key=key,
                base_url=self.base_url,
                max_retries=0,
                timeout=request_timeout_seconds,
            )
        self.client = client

    def complete(
        self,
        *,
        instructions: str,
        prompt: str,
        schema: Mapping[str, Any],
    ) -> MaaSResponse:
        schema_text = json.dumps(dict(schema), ensure_ascii=False, sort_keys=True)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": f"{instructions}\nReturn JSON matching this schema:\n{schema_text}",
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            max_tokens=self.max_output_tokens,
            stream=False,
            timeout=self.request_timeout_seconds,
        )
        choices = field_value(response, "choices", ()) or ()
        if not choices:
            raise ValueError("Zhipu returned no completion choices")
        choice = choices[0]
        finish_reason = field_value(choice, "finish_reason")
        if finish_reason != "stop":
            raise ValueError(f"Zhipu response was incomplete: {finish_reason or 'unknown'}")
        message = field_value(choice, "message")
        output = field_value(message, "content")
        if not isinstance(output, str) or not output.strip():
            raise ValueError("Zhipu returned no structured decision")
        usage = normalized_usage(
            field_value(response, "usage"),
            input_name="prompt_tokens",
            output_name="completion_tokens",
        )
        return MaaSResponse(output, usage)
