from __future__ import annotations

import json
import os
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from agent_loop.context import AgentContext
from agent_loop.maas_agent import MaaSAgent
from agent_loop.providers import MaaSResponse, create_provider
from agent_loop.providers.zhipu import (
    ZHIPU_CODING_BASE_URL,
    ZhipuCodingPlanProvider,
)
from agent_loop.types import DecisionKind


def context() -> AgentContext:
    return AgentContext("bounded prompt", "goal", (), None, None, (), {}, {})


def decision_payload() -> dict:
    return {
        "kind": "tool_call",
        "summary": "write the answer",
        "tool": "write_file",
        "arguments": json.dumps({"path": "answer.txt", "content": "done"}),
        "reason": None,
    }


class FakeProvider:
    name = "fake-maas"
    model = "fake-model"

    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[dict] = []

    def complete(self, **kwargs) -> MaaSResponse:
        self.calls.append(kwargs)
        return MaaSResponse(self.output, {"total_tokens": 9})


class FakeCompletions:
    def __init__(self, response) -> None:
        self.response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeZhipuClient:
    def __init__(self, response) -> None:
        self.chat = SimpleNamespace(completions=FakeCompletions(response))


class MaaSAgentTests(unittest.TestCase):
    def test_provider_independent_agent_validates_the_decision(self) -> None:
        provider = FakeProvider(json.dumps(decision_payload()))
        agent = MaaSAgent(provider)

        decision = agent.next_action(context())

        self.assertEqual(agent.provider_name, "fake-maas")
        self.assertEqual(agent.model, "fake-model")
        self.assertEqual(agent.last_usage, {"total_tokens": 9})
        self.assertEqual(decision.kind, DecisionKind.TOOL_CALL)
        self.assertEqual(decision.arguments["path"], "answer.txt")
        self.assertEqual(provider.calls[0]["prompt"], "bounded prompt")
        self.assertIn("arguments", provider.calls[0]["schema"]["properties"])

    def test_non_object_arguments_are_rejected_after_provider_output(self) -> None:
        payload = decision_payload()
        payload["arguments"] = "[]"
        agent = MaaSAgent(FakeProvider(json.dumps(payload)))

        with self.assertRaisesRegex(ValueError, "arguments must be an object"):
            agent.next_action(context())


class ZhipuCodingPlanProviderTests(unittest.TestCase):
    def test_chat_completion_is_normalized_into_an_agent_decision(self) -> None:
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=json.dumps(decision_payload())),
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=11,
                completion_tokens=7,
                total_tokens=18,
            ),
        )
        client = FakeZhipuClient(response)
        provider = ZhipuCodingPlanProvider("GLM-5.1", 12, 321, client=client)
        agent = MaaSAgent(provider)

        decision = agent.next_action(context())

        self.assertEqual(decision.kind, DecisionKind.TOOL_CALL)
        self.assertEqual(agent.last_usage["input_tokens"], 11)
        self.assertEqual(agent.last_usage["output_tokens"], 7)
        request = client.chat.completions.calls[0]
        self.assertEqual(request["model"], "GLM-5.1")
        self.assertEqual(request["max_tokens"], 321)
        self.assertEqual(request["timeout"], 12)
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertEqual(request["messages"][1]["content"], "bounded prompt")
        self.assertIn("additionalProperties", request["messages"][0]["content"])

    def test_non_stop_completion_is_never_used_as_an_action(self) -> None:
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="length",
                    message=SimpleNamespace(content=json.dumps(decision_payload())),
                )
            ]
        )
        agent = MaaSAgent(
            ZhipuCodingPlanProvider(
                "GLM-5.1", 12, 20, client=FakeZhipuClient(response)
            )
        )

        with self.assertRaisesRegex(ValueError, "length"):
            agent.next_action(context())

    def test_default_client_uses_coding_endpoint_and_disables_retries(self) -> None:
        constructor = Mock(return_value=SimpleNamespace())
        module = ModuleType("openai")
        module.OpenAI = constructor
        with (
            patch.dict(sys.modules, {"openai": module}),
            patch.dict(os.environ, {"ZAI_API_KEY": "test-secret"}),
        ):
            ZhipuCodingPlanProvider("GLM-5.1", 12, 321)

        constructor.assert_called_once_with(
            api_key="test-secret",
            base_url=ZHIPU_CODING_BASE_URL,
            max_retries=0,
            timeout=12,
        )

    def test_missing_api_key_is_reported_before_importing_the_sdk(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "ZAI_API_KEY"):
                ZhipuCodingPlanProvider("glm-5.1", 12, 321)

    def test_unknown_provider_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown MaaS provider"):
            create_provider("missing", "model", 10, 100)


if __name__ == "__main__":
    unittest.main()
