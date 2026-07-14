"""与厂商无关的 MaaS Provider 契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class MaaSResponse:
    """经过 Provider 归一化、供 Agent 适配器消费的响应。"""

    output_text: str
    usage: Mapping[str, int] = field(default_factory=dict)


class MaaSProvider(Protocol):
    """把一次有界 Prompt 翻译为结构化模型响应。"""

    name: str
    model: str

    def complete(
        self,
        *,
        instructions: str,
        prompt: str,
        schema: Mapping[str, Any],
    ) -> MaaSResponse: ...


def field_value(value: Any, name: str, default: Any = None) -> Any:
    """统一读取 SDK 对象和 Mapping 形态的测试替身。"""

    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def normalized_usage(
    usage: Any,
    *,
    input_name: str,
    output_name: str,
) -> dict[str, int]:
    """归一化各 Provider 的 Token 字段，但不虚构服务端未返回的数据。"""

    if usage is None:
        return {}
    aliases = {
        "input_tokens": input_name,
        "output_tokens": output_name,
        "total_tokens": "total_tokens",
    }
    values = {
        target: field_value(usage, source) for target, source in aliases.items()
    }
    return {name: value for name, value in values.items() if isinstance(value, int)}
