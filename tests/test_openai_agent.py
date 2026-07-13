from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from agent_loop.context import AgentContext
from agent_loop.openai_agent import OpenAIResponsesAgent
from agent_loop.types import DecisionKind


class FakeResponses:
    def __init__(self, response) -> None:
        self.response = response
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeClient:
    def __init__(self, response) -> None:
        self.responses = FakeResponses(response)


def context() -> AgentContext:
    return AgentContext("bounded prompt", "goal", (), None, None, (), {}, {})


class OpenAIResponsesAgentTests(unittest.TestCase):
    def test_requests_strict_bounded_output_and_parses_tool_call(self) -> None:
        payload = {
            "kind": "tool_call",
            "summary": "write the answer",
            "tool": "write_file",
            "arguments": json.dumps({"path": "answer.txt", "content": "done"}),
            "reason": None,
        }
        response = SimpleNamespace(
            status="completed",
            output_text=json.dumps(payload),
            output=[],
            usage=SimpleNamespace(input_tokens=10, output_tokens=8, total_tokens=18),
        )
        client = FakeClient(response)
        agent = OpenAIResponsesAgent("test-model", 7, 123, client=client)

        decision = agent.next_action(context())

        self.assertEqual(decision.kind, DecisionKind.TOOL_CALL)
        self.assertEqual(decision.arguments["path"], "answer.txt")
        request = client.responses.calls[0]
        self.assertEqual(request["model"], "test-model")
        self.assertEqual(request["input"], "bounded prompt")
        self.assertEqual(request["max_output_tokens"], 123)
        self.assertEqual(request["timeout"], 7)
        self.assertIs(request["store"], False)
        self.assertIs(request["text"]["format"]["strict"], True)
        self.assertEqual(agent.last_usage["total_tokens"], 18)

    def test_incomplete_response_is_never_used_as_an_action(self) -> None:
        response = SimpleNamespace(
            status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            output_text='{"kind": "tool_call"}',
            output=[],
        )
        agent = OpenAIResponsesAgent("test-model", 7, 20, client=FakeClient(response))

        with self.assertRaisesRegex(ValueError, "max_output_tokens"):
            agent.next_action(context())

    def test_refusal_is_reported(self) -> None:
        refusal = SimpleNamespace(type="refusal", refusal="cannot comply")
        item = SimpleNamespace(content=[refusal])
        response = SimpleNamespace(status="completed", output=[item], output_text="")
        agent = OpenAIResponsesAgent("test-model", 7, 20, client=FakeClient(response))

        with self.assertRaisesRegex(ValueError, "model refusal"):
            agent.next_action(context())


if __name__ == "__main__":
    unittest.main()
