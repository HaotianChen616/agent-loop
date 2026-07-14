"""显式注册的工具是 Agent 能够触发副作用的唯一入口。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .types import AgentLoopError, RiskLevel, ToolResult, ToolStatus
from .workspace import Workspace


# 这里同时是传给 Agent 的工具说明。参数契约保持封闭，未知字段会被拒绝。
TOOL_METADATA: dict[str, dict[str, Any]] = {
    "list_files": {
        "description": "List workspace file paths.",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 1000}},
            "required": [],
            "additionalProperties": False,
        },
    },
    "read_file": {
        "description": "Read one UTF-8 text file from the workspace.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "write_file": {
        "description": "Replace one UTF-8 text file inside the workspace.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    "mock_external_write": {
        "description": "Simulate an external write for the approval lesson.",
        "parameters": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
    },
}


class Tool(Protocol):
    """所有工具都必须声明风险级别，以及是否会修改 Workspace。"""

    name: str
    risk: RiskLevel
    mutates_workspace: bool

    def run(self, arguments: Mapping[str, Any], workspace: Workspace) -> tuple[str, Any, bool]: ...


def _validate_arguments(
    arguments: Mapping[str, Any], required: set[str], optional: set[str] | None = None
) -> None:
    optional = optional or set()
    missing = required - set(arguments)
    unknown = set(arguments) - required - optional
    if missing or unknown:
        raise ValueError(f"invalid arguments; missing={sorted(missing)}, unknown={sorted(unknown)}")


@dataclass(frozen=True)
class ListFilesTool:
    name: str = "list_files"
    risk: RiskLevel = RiskLevel.READ
    mutates_workspace: bool = False

    def run(self, arguments: Mapping[str, Any], workspace: Workspace) -> tuple[str, Any, bool]:
        _validate_arguments(arguments, set(), {"limit"})
        limit = arguments.get("limit", 200)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1_000:
            raise ValueError("limit must be an integer between 1 and 1000")
        files = workspace.list_files(limit=limit)
        return f"listed {len(files)} files", files, False


@dataclass(frozen=True)
class ReadFileTool:
    max_output_chars: int
    name: str = "read_file"
    risk: RiskLevel = RiskLevel.READ
    mutates_workspace: bool = False

    def run(self, arguments: Mapping[str, Any], workspace: Workspace) -> tuple[str, Any, bool]:
        _validate_arguments(arguments, {"path"})
        path = arguments["path"]
        if not isinstance(path, str):
            raise ValueError("path must be a string")
        content = workspace.read_text(path, max_chars=1_000_000)
        truncated = len(content) > self.max_output_chars
        output = content[: self.max_output_chars]
        return f"read {path}", output, truncated


@dataclass(frozen=True)
class WriteFileTool:
    name: str = "write_file"
    risk: RiskLevel = RiskLevel.LOCAL_WRITE
    mutates_workspace: bool = True

    def run(self, arguments: Mapping[str, Any], workspace: Workspace) -> tuple[str, Any, bool]:
        _validate_arguments(arguments, {"path", "content"})
        path, content = arguments["path"], arguments["content"]
        if not isinstance(path, str) or not isinstance(content, str):
            raise ValueError("path and content must be strings")
        written = workspace.write_text(path, content)
        return f"wrote {written} bytes to {path}", {"bytes_written": written}, False


@dataclass(frozen=True)
class MockExternalWriteTool:
    """在不访问真实外部系统的情况下演示人工审批路径。"""

    name: str = "mock_external_write"
    risk: RiskLevel = RiskLevel.EXTERNAL_WRITE
    mutates_workspace: bool = False

    def run(self, arguments: Mapping[str, Any], workspace: Workspace) -> tuple[str, Any, bool]:
        del workspace
        _validate_arguments(arguments, {"message"})
        if not isinstance(arguments["message"], str):
            raise ValueError("message must be a string")
        return "simulated an approved external write", {"sent": False}, False


class ToolRegistry:
    """持有可信工具实现，并把不可信参数转换为结构化 ToolResult。"""

    def __init__(self, workspace: Workspace, max_output_chars: int = 8_000) -> None:
        self.workspace = workspace
        tools: tuple[Tool, ...] = (
            ListFilesTool(),
            ReadFileTool(max_output_chars),
            WriteFileTool(),
            MockExternalWriteTool(),
        )
        self._tools = {tool.name: tool for tool in tools}

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ValueError(f"unknown tool: {name}") from exc

    def definitions(self, allowed: tuple[str, ...]) -> tuple[dict[str, Any], ...]:
        definitions = []
        for name in allowed:
            tool = self.get(name)
            definitions.append(
                {
                    "name": name,
                    "risk": tool.risk.value,
                    "mutates_workspace": tool.mutates_workspace,
                    **TOOL_METADATA[name],
                }
            )
        return tuple(definitions)

    def execute(self, action_id: str, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        started = time.monotonic()
        try:
            tool = self.get(name)
            summary, output, truncated = tool.run(arguments, self.workspace)
            status, error = ToolStatus.SUCCESS, None
        except (AgentLoopError, OSError, UnicodeError, ValueError) as exc:
            summary, output, truncated = str(exc), None, False
            status, error = ToolStatus.ERROR, type(exc).__name__
        return ToolResult(
            action_id=action_id,
            tool_name=name,
            status=status,
            summary=summary,
            output=output,
            error_code=error,
            duration_ms=int((time.monotonic() - started) * 1_000),
            output_truncated=truncated,
        )
