"""Provider-neutral contracts for model-as-a-service calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class MaaSResponse:
    """A provider response normalized for the Agent adapter."""

    output_text: str
    usage: Mapping[str, int] = field(default_factory=dict)


class MaaSProvider(Protocol):
    """Translate one bounded prompt into one structured model response."""

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
    """Read SDK objects and mapping-shaped test doubles uniformly."""

    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def normalized_usage(
    usage: Any,
    *,
    input_name: str,
    output_name: str,
) -> dict[str, int]:
    """Normalize provider token names without inventing missing values."""

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
