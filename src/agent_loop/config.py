"""从 TOML 加载并校验教学 Scenario。"""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import Any, Mapping

from .providers import PROVIDER_NAMES
from .types import (
    AgentSpec,
    BudgetLimits,
    ConfigError,
    ContextSpec,
    PolicySpec,
    RiskLevel,
    RunSpec,
    VerificationSpec,
    WorkspaceSpec,
)


def _table(data: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, Mapping):
        raise ConfigError(f"{key} must be a TOML table")
    return value


def _strings(data: Mapping[str, Any], key: str, *, required: bool = False) -> tuple[str, ...]:
    value = data.get(key)
    if value is None and not required:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ConfigError(f"{key} must be a list of non-empty strings")
    return tuple(value)


def _text(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string")
    return value.strip()


def _positive(data: Mapping[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{key} must be a positive integer")
    return value


def _risk_levels(data: Mapping[str, Any], key: str, default: tuple[RiskLevel, ...]) -> tuple[RiskLevel, ...]:
    raw = data.get(key)
    if raw is None:
        return default
    try:
        return tuple(RiskLevel(value) for value in _strings(data, key))
    except ValueError as exc:
        raise ConfigError(f"{key} contains an unknown risk level") from exc


def _scenario_path(root: Path, value: str, label: str, *, directory: bool = False) -> Path:
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ConfigError(f"{label} must stay inside the scenario directory")
    resolved = (root / relative).resolve()
    expected = resolved.is_dir() if directory else resolved.is_file()
    if not resolved.is_relative_to(root) or not expected:
        raise ConfigError(f"{label} does not resolve to a valid scenario path: {value}")
    return resolved


def _scenario_digest(root: Path) -> str:
    """将 Run 绑定到 Scenario 控制目录中的全部稳定文件。"""

    files: set[Path] = set()
    for entry in root.rglob("*"):
        relative = entry.relative_to(root)
        if entry.is_symlink():
            raise ConfigError("scenario cannot contain symbolic links")
        if "__pycache__" in relative.parts or entry.suffix == ".pyc":
            continue
        if entry.is_file():
            files.add(entry)
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        name = path.relative_to(root).as_posix()
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def load_run_spec(path: str | Path) -> RunSpec:
    """读取 Scenario，并冻结解析后的路径与内容摘要。"""

    scenario_file = Path(path).expanduser().resolve()
    if not scenario_file.is_file():
        raise ConfigError(f"scenario file does not exist: {scenario_file}")
    raw = scenario_file.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"invalid scenario TOML: {exc}") from exc

    if data.get("schema_version") != 1:
        raise ConfigError("only scenario schema_version = 1 is supported")
    criteria = _strings(data, "acceptance_criteria", required=True)
    if not criteria or len(criteria) != len(set(criteria)):
        raise ConfigError("acceptance_criteria must be non-empty and unique")

    # Scenario 内引用只能相对当前目录解析，不能借配置读取仓库中的任意文件。
    root = scenario_file.parent
    workspace_data = _table(data, "workspace")
    seed = _scenario_path(root, _text(workspace_data, "seed"), "workspace.seed", directory=True)
    if workspace_data.get("mode", "copy") != "copy":
        raise ConfigError("v0 only supports workspace.mode = 'copy'")

    verification_data = _table(data, "verification")
    script = _scenario_path(root, _text(verification_data, "script"), "verification.script")
    if verification_data.get("kind", "python_script") != "python_script":
        raise ConfigError("v0 only supports verification.kind = 'python_script'")

    context_data = _table(data, "context")
    agent_data = _table(data, "agent")
    budget_data = _table(data, "budget")
    policy_data = _table(data, "policy")
    # 三个风险集合必须互斥，否则同一动作会同时得到矛盾的授权结论。
    policy = PolicySpec(
        auto_allow=_risk_levels(policy_data, "auto_allow", PolicySpec().auto_allow),
        require_approval=_risk_levels(
            policy_data, "require_approval", PolicySpec().require_approval
        ),
        deny=_risk_levels(policy_data, "deny", PolicySpec().deny),
    )
    if any(
        left & right
        for left, right in (
            (set(policy.auto_allow), set(policy.require_approval)),
            (set(policy.auto_allow), set(policy.deny)),
            (set(policy.require_approval), set(policy.deny)),
        )
    ):
        raise ConfigError("policy risk groups must not overlap")

    instructions = _strings(data, "instructions")
    for instruction in instructions:
        _scenario_path(root, instruction, "instructions")
    allowed_tools = _strings(data, "allowed_tools", required=True)
    known_tools = {"list_files", "read_file", "write_file", "mock_external_write"}
    if not allowed_tools or len(allowed_tools) != len(set(allowed_tools)) or not set(allowed_tools) <= known_tools:
        raise ConfigError("allowed_tools must be a non-empty unique subset of the v0 tools")
    agent_script = agent_data.get("script")
    if agent_script is not None:
        if not isinstance(agent_script, str):
            raise ConfigError("agent.script must name an existing scenario file")
        agent_script = str(_scenario_path(root, agent_script, "agent.script"))
    agent_kind = agent_data.get("kind", "scripted")
    if agent_kind not in {"scripted", "llm"}:
        raise ConfigError("agent.kind must be 'scripted' or 'llm'")
    agent_provider = agent_data.get("provider")
    if agent_provider is not None and (
        not isinstance(agent_provider, str) or not agent_provider.strip()
    ):
        raise ConfigError("agent.provider must be a non-empty string")
    if agent_kind == "scripted":
        if agent_provider is not None:
            raise ConfigError("agent.provider can only be used with agent.kind = 'llm'")
        agent_provider = None
    else:
        agent_provider = agent_provider or "openai"
        if agent_provider not in PROVIDER_NAMES:
            raise ConfigError(f"unknown agent.provider: {agent_provider}")
    agent_model = agent_data.get("model")
    if agent_model is not None and (
        not isinstance(agent_model, str) or not agent_model.strip()
    ):
        raise ConfigError("agent.model must be a non-empty string")

    # RunSpec 只包含校验后的不可变配置；运行期不再直接依赖原始 TOML 字典。
    return RunSpec(
        schema_version=1,
        scenario_id=_text(data, "scenario_id"),
        title=_text(data, "title"),
        learning_objective=_text(data, "learning_objective"),
        goal=_text(data, "goal"),
        acceptance_criteria=criteria,
        instructions=instructions,
        allowed_tools=allowed_tools,
        context=ContextSpec(
            _positive(context_data, "max_input_chars", 30_000),
            _positive(context_data, "max_history_items", 8),
            _positive(context_data, "max_tool_output_chars", 8_000),
        ),
        workspace=WorkspaceSpec(
            str(seed), "copy", _strings(workspace_data, "read_only")
        ),
        agent=AgentSpec(
            kind=str(agent_kind),
            request_timeout_seconds=_positive(
                agent_data, "request_timeout_seconds", 30
            ),
            max_output_tokens=_positive(agent_data, "max_output_tokens", 1_000),
            model=agent_model,
            script=agent_script,
            provider=agent_provider,
        ),
        verification=VerificationSpec(
            str(script),
            str(verification_data.get("kind", "python_script")),
            _positive(verification_data, "timeout_seconds", 10),
        ),
        budget=BudgetLimits(
            _positive(budget_data, "max_iterations", 6),
            _positive(budget_data, "max_agent_calls", 6),
            _positive(budget_data, "max_tool_calls", 10),
            _positive(budget_data, "max_verifications", 7),
            _positive(budget_data, "max_elapsed_seconds", 120),
            _positive(budget_data, "max_same_failure", 2),
        ),
        policy=policy,
        scenario_root=str(root),
        scenario_file=str(scenario_file),
        digest=_scenario_digest(root),
    )
